# Known server limitations

- Exact mailbox used/quota bytes are not a dependable standard-EWS feature. The client displays
  `Quota unavailable` instead of presenting folder sums as quota.
- The independent core-mail probe detected Exchange build `15.1.2375.7`. Advanced account permissions remain unprobed.
- ResolveNames, free/busy, archive folders, recoverable items, rules, permissions, and directory
  visibility can be disabled or restricted independently.
- Exchange IDs and change keys can change after move/copy and cannot be recreated during restore.
- SendItem does not provide a reliable Sent Items ID. Ambiguous sends require reconciliation.
- FindPeople paging and some Exchange builds have server/library-specific behavior; this client
  uses bounded requests and does not assume full directory enumeration.
- EWS has no globally atomic mailbox snapshot. Backups record this limitation.
- Native deferred-send property support is not assumed. The selected product strategy is the
  durable local scheduler.
- Recurrence, exceptions, time zones, archive access, and recoverable-item semantics vary by
  Exchange build.
- Administrative discovery, hold, application-management, retention-tag, delegate mutation,
  conversation action, user-photo, reminder action, and account-configuration gaps remain
  classified but not implemented.

Core mail works against the GUC server: folder and message browsing, server drafts, sending,
reply/forward, attachments, and move/delete/restore. Known GUC-specific limits:

- The quota-property request does not return quota thresholds, so exact limits may show as
  unavailable (see [Mailbox storage](../features/storage-and-quota.md)).
- A free/busy (availability) request for your own mailbox can return an error.

Rules, push subscriptions, and administrative operations depend on your account's permissions.
If a feature is refused, the client reports it as unsupported or unauthorized and changes nothing.

