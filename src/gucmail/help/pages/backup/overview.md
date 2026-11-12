# Create and verify a private backup

A backup is an independent, self-describing archive. It is not the disposable cache or search index. It can contain complete private messages, calendar items, contacts, tasks, and attachments according to the selected scope.

## Safe workflow

1. Review the metadata-only size estimate and check the destination's free disk space.
2. Choose a bounded folder or item scope. Creation downloads its bodies and supported attachments.
3. Enable encryption for removable, shared, or synchronized storage. Keep the passphrase separately.
4. Create or resume into an incomplete directory; completed content objects are addressed by cryptographic hash.
5. Verify manifest schema, object hashes, counts, encryption metadata, and parent linkage.

Exchange cannot provide a globally atomic mailbox snapshot, so the manifest records timing and consistency limits. Incrementals are parent-linked and must keep every required ancestor.

For an incremental in the Backup Center, highlight its complete parent archive. The confirmation shows that parent's ID; its saved scope and encryption are inherited. Changing destination or scope requires a new base. Encrypted parent estimates also request the passphrase. In the CLI, use `gucmail backup incremental BACKUP_ID` (add `--encrypted` for an encrypted parent).

Selected-message, search-result and cluster backups preserve a fixed set of references. Incrementals revisit those references rather than re-running the search. They rescan that complete scope in bounded pages and store changes; they do not use native Exchange change tracking. New archive chains verify the actual mailbox identity before continuation or resume. Unknown local references and interrupted scans fail safely instead of being assumed deleted.

Restore is plan-first. It creates supported items without sending restored drafts or recreating historical Exchange IDs. Recurrence or extended fields that cannot be preserved must be reported, not silently dropped. Continue with [Offline backup browsing](help:backup.offline).

Calendar, contact, and Task items with embedded item attachments use a private opaque Exchange
item stream. Their selected metadata can be browsed offline; the stream preserves the containing
item without converting embedded items to files. Individual embedded contents cannot be extracted
from that stream locally. Export/import requires Exchange 2010 SP1 or newer, bounded item-size
metadata, and server permission that has not been live-verified. File-only PIM backups retain their
portable format. An unsupported or incomplete export leaves a resumable incomplete archive.

The restore plan identifies native stream items. Confirmed restoration creates a new item in the
dedicated folder and verifies its new type and identity. It does not reestablish original mailbox
relationships or prove complete historical fidelity. If verification fails after acceptance,
Exchange may already have created the item. Inspect the destination before planning another
restore. The result screen disables replay after an attempt, and CLI item failures return exit 10
with their structured partial result. Use Tab and keyboard scrolling to reach long warnings and
controls in a small terminal.

To stop an active restore, press Ctrl+C. The current item may finish, then later items
are skipped. Reaching the restore time limit also requests a stop and keeps the current
result. If stopped before the first request starts, restore shows zero attempts.
Escape or Close while it is active keeps the screen open until the pending
result arrives. Completed and uncertain item results remain available, and Restore stays
disabled for that attempt. A CLI interruption during preparation or creation returns exit
130 after printing the available partial results. Inspect the destination before starting
another plan; abrupt process termination cannot supply this result.

The same uncertainty warning applies to ordinary MIME and field-based restores. A lost create
response, missing returned identity, failed attachment upload or failed local-reference save may
leave a created item in Exchange. The operation is not retried automatically. Local archive-read
failures keep the results of earlier restored items. Restore stops at the first item error by
default; inspect the destination and partial results before confirming another plan.
