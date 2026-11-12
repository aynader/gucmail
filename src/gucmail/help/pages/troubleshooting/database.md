# Cache, database, and Deleted Items errors

The durable state database owns references, drafts, schedules, checkpoints, and history. Cache and the search index are separate. Clearing cache is not a repair for every database error.

## Locked or corrupt state

Stop duplicate TUI, watcher, and scheduler processes, then retry one health check. Migration and initialization use transactions and a schema version. If integrity verification fails, preserve the file for diagnostics and use the application's scoped recovery path rather than deleting a broad directory.

## Deleted Items and stale references

Moving or deleting an Exchange item can replace its server ID and change key. The local reference must upsert that new identity transactionally. A uniqueness conflict or stale mapping should not make the whole mailbox unavailable. Refresh the affected folder once and inspect the translated error before another mutation.

Do not repeatedly delete, restore, or move a real item to probe an integrity failure. Reproduce identity changes with synthetic gateway objects and a temporary database. A verified backup is separate from application-state recovery and cannot recreate exact historical Exchange identifiers.
