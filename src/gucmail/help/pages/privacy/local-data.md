# Local data, logs, indexes, and integration privacy

The application stores different kinds of local data for different purposes. Removing one must not silently remove another.

## Storage boundaries

- Preferences contain non-secret configuration.
- The keyring contains selected secrets.
- Operational state contains references, drafts, schedules, checkpoints, and delivery history.
- Cache is disposable acceleration data.
- The search index is rebuildable derived text.
- Backups are intentionally retained private archives.
- Logs and diagnostics contain redacted operational metadata.

Message-body caching, attachment indexing, semantic search, monitoring, command hooks, and external integrations are disabled until configured. Indexes and backups can still contain private text, even without a password.

Normal logs exclude message bodies, attachment bytes, authorization headers, cookies, and raw SOAP. Review every diagnostic bundle before sharing. Webhooks and Discord can send allowlisted data off the computer; desktop notifications can reveal it on a lock screen. No cloud AI service is required for Help, palette interpretation, fuzzy search, or lexical indexing.

Use platform path commands to inspect exact locations. Stop workers and verify the target before any local cleanup.
