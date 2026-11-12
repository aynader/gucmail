# Delete, Deleted Items, and restore

Ordinary deletion should move a message to Deleted Items when Exchange permits it. That is different from permanent deletion and from a recoverable-items store that the account may not be allowed to browse.

## Safer workflow

1. Preview the selected references, current folder, and count.
2. Use normal Delete unless permanent removal is specifically required.
3. Open Deleted Items and verify the result.
4. Restore by selecting a destination and reviewing the move plan.

A move can replace the Exchange item ID and change key. The local reference layer must update that mapping transactionally rather than inserting a conflicting second identity. If Deleted Items reports a database or stale-reference error, stop repeating the mutation, refresh once, and read [Database and Deleted Items errors](help:troubleshooting.database).

Permanent deletion may not be recoverable. It needs a separate, explicit irreversible path and is never the bulk default. A backup can preserve content but cannot recreate historical Exchange IDs, exact delivery properties, or every server-side feature. See [Create a private backup](help:backup.overview).
