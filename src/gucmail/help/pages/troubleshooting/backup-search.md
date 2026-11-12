# Backup, offline browsing, or search is not working

Backup, cache, and index have different recovery rules. A cache and index are disposable derivatives; a backup is a durable archive that must remain verifiable. Never delete or overwrite an archive merely because a search result is missing.

## A backup is stuck or incomplete

Run `gucmail backup inspect BACKUP_ID` and identify the archive state, last durable checkpoint, free disk space, selected scope, and most recent error. Replace BACKUP_ID with the archive ID shown by `gucmail backup list`. An incomplete backup directory is resumable only when its manifest, options, source identity, and completed content objects still match. Use `gucmail backup resume BACKUP_ID` rather than starting many overlapping copies.

Network interruption, sleep, server throttling, a changed source item, unavailable attachment, insufficient disk space, and an unavailable encryption key are reported separately. Resume keeps completed content-addressed objects and refetches only what cannot be trusted. Never mark an incomplete archive verified.

After completion, run `gucmail backup verify BACKUP_ID`. Verification checks schema, parent linkage, hashes, sizes, counts, and encryption metadata without contacting Exchange. A failed hash means the affected object cannot be trusted; keep the archive for investigation and create a separate replacement if needed.

## Offline browsing cannot open an item

Confirm that the selected backup is complete and verified, that every incremental parent remains available, and that the encryption key can be unlocked. Some backups intentionally exclude bodies or attachment content; metadata can exist even when the full object was never included. The browser should show that distinction rather than pretending corruption.

Restore is plan-first. Use `gucmail backup restore-plan BACKUP_ID` to list supported items, conflicts, duplicates, required connectivity, and fields that cannot be reproduced. Restore never sends historical drafts automatically and never promises to recreate old Exchange IDs.

If an applied restore fails, inspect its per-item partial results. An uncertain result means Exchange
may already have created that item, even when the attachment upload or local-reference save failed.
Do not repeat the plan until you have inspected the dedicated destination. Restore makes no
automatic retry and stops on the first item error by default. Local archive-read failures retain
earlier successful results and do not imply that the failed item was created.

If you stop a TUI restore with Ctrl+C, wait for the current request to finish. Later items
will not start. A restore time limit requests the same stop; it does not discard the
current result or prove that Exchange cancelled its request. A queued restore stopped
before the first request starts shows zero attempts. Escape or Close also keeps a pending
restore open so its result is retained.
The current item may have been created even when its response was interrupted. CLI Ctrl+C
during an item prints partial results and returns exit 130. Keep those results and inspect
the dedicated destination before creating another restore plan. A killed process may have
no final result; do not assume that it rolled back.

## Search cannot find a known message

First identify the search source. Live Exchange search reflects current server content. A local index reflects its last update checkpoint and configured body or attachment scope. Backup search reflects one archive point in time.

For live search, simplify the query and confirm the selected folder and date range. Unsupported server filters may use a clearly bounded client fallback. For local search, run `gucmail index status`, compare the checkpoint with the message date, and check whether the relevant folder, body, or attachment format was indexed. Fuzzy search can find spelling variants but exact automation should use exact fields.

Rebuild an index only after its verification or health check proves repair is needed. Rebuilding consumes local time and disk but does not alter Exchange. Clearing cache does not repair or erase a backup. Search examples and source semantics are in [Search mail](help:search.overview) and [Local index](help:search.local).

## Disk use is unexpectedly high

Open Storage and compare cache, index, incomplete backup, completed backup, log, and recovery-draft sizes separately. Pruning a backup requires a preview because incrementals may depend on parents. Clear or rebuild only disposable data with the corresponding command and confirmation. Never use broad filesystem deletion as a substitute for application-level cleanup.

Safe diagnostic data includes archive ID, schema version, verification result, counts, hash error class, index checkpoint, selected search source, query plan, and local byte totals. Exclude archive encryption keys, message content, attachment content, credentials, and private paths from shared reports.
