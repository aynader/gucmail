# Feature and EWS support status

This matrix separates four questions that must not be collapsed:

1. Is a typed implementation present?
2. Is the detected server version eligible?
3. Does the authenticated account have access?
4. How was the path verified?

The registry defaults describe an unprobed installation. An independent, conservatively bounded
GUC verification has since exercised core mail.
Those results do not infer advanced permissions for this or any other account. Inspect an installed
capability record with:

```text
gucmail --json ews support-matrix
gucmail ews describe OPERATION
```

The current typed registry contains 93 classified capabilities: 38 implemented, 10 partial, and
45 not implemented. These are source classifications, not a completion percentage or proof of
current server eligibility, account access, or every product workflow.

Library availability alone is not an implemented product workflow. Dynamic server support and
account access remain `not_probed` until a capability-specific probe records evidence.

| Family | Entries | Implementation status |
|---|---:|---|
| Account | 2 | 2 not implemented |
| Applications | 3 | 3 not implemented |
| Archive | 1 | 1 partial |
| Attachments | 3 | 3 implemented |
| Automatic Replies | 2 | 2 implemented |
| Calendar | 11 | 6 implemented, 1 partial, 4 not implemented |
| Contacts | 5 | 3 implemented, 2 not implemented |
| Conversations | 3 | 3 not implemented |
| Delegation | 4 | 1 implemented, 3 not implemented |
| Directory | 2 | 1 implemented, 1 not implemented |
| Discovery | 5 | 5 not implemented |
| Extended Properties | 1 | 1 partial |
| Folders | 9 | 8 implemented, 1 partial |
| Items | 12 | 8 implemented, 2 partial, 2 not implemented |
| Mail Tips | 1 | 1 not implemented |
| Messages | 2 | 1 implemented, 1 not implemented |
| Notes | 1 | 1 not implemented |
| Notifications | 6 | 6 not implemented |
| People | 1 | 1 not implemented |
| Permissions | 2 | 2 not implemented |
| Quota | 2 | 1 partial, 1 not implemented |
| Recovery | 1 | 1 not implemented |
| Retention | 1 | 1 not implemented |
| Rules | 2 | 1 implemented, 1 partial |
| Scheduling | 1 | 1 partial |
| Synchronization | 2 | 1 implemented, 1 not implemented |
| Tasks | 4 | 3 implemented, 1 partial |
| User Configuration | 4 | 4 not implemented |

## Product feature matrix

| Product feature | Status | Important boundary |
|---|---|---|
| Mail/folder CRUD, reply, forward, MIME, attachments | Implemented | Core mail works against the GUC server; folder create/rename/delete and raw MIME have not been tested there |
| Durable drafts and recovery | Implemented locally | Server draft sync is bounded |
| Automatic replies | Implemented | Shared CLI/TUI policy editor, stale-preview protection and readback; installed-library/offline-tested; current server/account permission remains unprobed |
| Exchange inbox rules | Bounded implementation | Read policies, create simple disabled rules, enable/disable and exact-ID delete with Outlook rule data preservation; arbitrary editing remains absent; current server/account access unprobed |
| Scheduled and recurring messages | Implemented locally | Computer/supervisor must run |
| Calendar CRUD, response, free/busy, ICS | Implemented | Typed recurrence with explicit occurrence/series choices; "this and future" scope and complete exception history remain unavailable |
| Mailbox delegates | Bounded read-only implementation | Reported identities and folder permissions only; no complete effective access/Send As inventory or delegate writes; current server/account support unprobed |
| Contacts CRUD, duplicates, merge, vCard, directory | Implemented | Merge deletes are recoverable; group items are distinguished from individual contacts |
| Contact groups | Bounded implementation | Account-local groups and personal mailbox SMTP-group CRUD, stale-preview checks and reviewed Bcc composition; nested/linked membership and directory-group management remain outside the editor; mailbox account support unprobed |
| Tasks CRUD and export | Implemented | Typed simple recurrence writes and simple backup restore exist; occurrence-scoped changes and complete recurrence-history restore remain unavailable |
| Exact mailbox quota | Capability-gated | Standard EWS often cannot return exact used/quota bytes |
| Full backup, parent-diff incremental snapshots, verify, offline browse, restore | Implemented | Incrementals compare bounded full snapshots rather than native EWS change tracking; restore is best-effort and historical IDs cannot be recreated |
| FTS5, fuzzy search, clusters, insights | Implemented locally | Bounded scans; index is disposable |
| Semantic search | Implemented locally with opt-in | Corpus/topic ranking uses the existing local index; freshness and field coverage are explicit. No model download or remote embedding service is used |
| Cleanup | Implemented preview-first | Recoverable delete only by default |
| Arbitrary raw SOAP | Unavailable | Expert mode and the unavailable Raw SOAP setting cannot enable an executor; named request previews are local classification, and supported actions use typed commands |

See [Known server limitations](../ews/known-server-limitations.md) and the [EWS architecture](../ews/index.md).
The [feature-by-feature operation matrix](../ews/operation-matrix.md) lists every classified entry
and every independent support axis.
