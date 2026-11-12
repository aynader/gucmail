# Local data locations and retention

Use `gucmail config path`, `gucmail cache status`, `gucmail index status`, and `gucmail backup list`
to inspect the paths selected by the current platform and configuration. Environment overrides such
as `GUCMAIL_DATA_DIR` can change them.

| Data | Purpose | Deletion behavior |
|---|---|---|
| OS keyring entry | Mailbox password | `gucmail logout` removes the selected entry |
| `config.toml` | Non-secret settings | Manual, scoped deletion |
| State SQLite database | references, drafts, schedules, checkpoints, notification ledger | Durable; newest 10,000 notification outcomes retained |
| Recovery directory | draft recovery journals | Remove only after reviewing drafts |
| Cache directory | disposable cached data | `gucmail cache clear` is scoped |
| FTS SQLite database | rebuildable local search index | `gucmail index clear` |
| Backup repository | independent private archives | `backup prune` moves eligible sets to local trash |
| Logs/diagnostics | redacted operational metadata | Review before sharing or deleting |
| Downloads | user-requested attachments/exports | User-managed |

Uninstalling the Python package does not promise to erase any of these locations. Backups can
contain complete private message and attachment data. Encryption protects at rest only while the
passphrase remains separate and strong.

Before deleting durable state, stop foreground workers and OS scheduler integration, inspect exact
absolute paths, and keep any backup needed for recovery. Never use a broad recursive delete against
a home directory or workspace root.
