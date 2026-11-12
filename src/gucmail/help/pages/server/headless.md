# Run monitoring on an always-on computer

An always-on private computer or VPS can monitor the mailbox while a laptop is asleep. The main PC does not need to stay on because the server runs the watcher and forwards allowed notifications. The server becomes a sensitive mailbox client: use a dedicated operating-system account, encrypted storage, restrictive permissions, supported secret storage, conservative polling, and security updates.

## Easy path

1. Install the same tested GUC Mail package in an isolated Python environment.
2. Copy or import only non-secret configuration. Do not copy a plaintext password or live session files.
3. Set up credentials through a non-interactive secret mechanism available to the service account.
4. Validate configuration locally:

   ```text
   gucmail headless validate
   gucmail status
   ```

5. Run exactly one foreground cycle. It establishes a quiet baseline unless existing-mail notification was explicitly selected:

   ```text
   gucmail headless run
   ```

6. Inspect watcher status and health:

   ```text
   gucmail headless status
   gucmail headless health
   ```

7. Configure a synthetic console, NDJSON, Discord, or generic-webhook test. Confirm one external delivery only when the destination is yours.
8. Generate and review the supervisor plan before applying any service change.

The watcher reuses one authenticated session, keeps a persistent checkpoint, suppresses duplicate event IDs, performs bounded catch-up after interruption, and respects server retry guidance. A default headless run performs one cycle; `gucmail headless run --forever` is intended for a trusted supervisor and stops cleanly on a signal.

## Advanced systemd path

On Linux, generate a secret-free systemd plan instead of hand-writing a shell command:

```text
gucmail startup plan --platform linux --system-service
gucmail startup enable --platform linux --system-service
```

The preview identifies the absolute executable, argument vector, service user, working and output directories, network ordering, restart delay, restart limit, single-instance profile, and managed unit content. Apply only after reviewing those values and the service account's credential access. Do not place a password in a unit, environment file, command line, or journal-visible argument.

If a system-wide service is unnecessary, use the generated user-service path and enable user lingering only after understanding its security effect. Use the platform's service manager to start, stop, enable, disable, and inspect the unit. GUC Mail's cooperative controls preserve durable state:

```text
gucmail headless pause
gucmail headless resume
gucmail headless stop
```

Do not force-terminate a healthy cycle unless the operating system requires it. A cooperative stop lets the current transaction finish and releases single-instance ownership.

## Credentials, logs, and health

The service identity must be able to unlock the intended keyring or other supported secret reference without borrowing an interactive user's unlocked desktop. If that cannot be secured, unattended operation is not available on that host. `creds.env` is a legacy development path, not a recommended server secret store.

Health output contains no mailbox content. Monitor process ownership, profile, paused/stopping state, heartbeat age, last successful cycle, checkpoint age, strategy, authentication status, backoff, and provider-delivery health. Treat a stale heartbeat or credential-required state as actionable. Keep logs bounded and redacted; never enable body or attachment logging.

Do not expose the TUI, SQLite database, logs, or diagnostic endpoint publicly. Generic webhooks are outbound. If a separate infrastructure monitor runs `gucmail headless health`, give it only the local execution rights it needs.

## Updating and removal

Before an update, request a cooperative stop, back up durable local state, install the tested package, run offline configuration and database migrations, validate headless configuration, then restart one supervised instance. Check the capability and migration reports before allowing external notifications.

To retire the host, disable and remove the managed service through the startup commands, verify that no watcher remains, remove credentials from the host's secret store, and securely handle logs, indexes, state, and backups according to their separate retention needs. Disabling the service must not delete server mail.

Use [provider routing](help:integrations.providers), [credential privacy](help:privacy.credentials), and [notification troubleshooting](help:troubleshooting.notifications) alongside this guide.
