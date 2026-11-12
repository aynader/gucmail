# Headless watcher deployment

Use the headless watcher when no Textual interface is running. It is a foreground, single-instance
process designed for an operating-system supervisor; GUC Mail CLI does not install a custom daemon
or hide a background process on its own.

## Prepare and validate

First configure credentials interactively for the same OS user that will run the service. Then
create and enable an account-scoped profile:

```text
gucmail notifications profiles create primary --name "Primary mailbox" --folder Inbox --activate
gucmail notifications profiles enable primary
gucmail headless validate --profile primary
```

`headless validate` does not open the mailbox. It checks configuration, credential availability,
directories, the profile, and provider policy without printing secrets. Run a bounded foreground
check before installing startup integration:

```text
gucmail headless run --profile primary
gucmail headless status --profile primary
gucmail headless health --profile primary
```

The first run is a quiet baseline unless the profile explicitly enables existing-message
notifications. A later run reuses the durable checkpoint, deduplication ledger, delivery outbox,
and status directory.

## Managed startup integration

Generate a secret-free preview first:

```text
gucmail startup plan --profile primary
gucmail startup enable --profile primary
```

`startup enable` is preview-only unless `--apply` is supplied (add the global `--yes` for
non-interactive confirmation, as in `gucmail --yes startup enable --profile primary --apply`).
Inspect the generated executable path, account scope, state directory, and service definition before
applying it. The installed definition contains no password, token, webhook URL, or message content;
the service resolves credentials from the OS keyring under its runtime user.

On Windows, the managed target is a Task Scheduler task named `GUC Mail Watcher (<id>)` that runs
with `pythonw.exe`, so no console window opens. On Linux, it is a user-level systemd unit.

Windows plans bind both the logon trigger and principal to the current process's Windows user
SID. Scheduled-send plans also bind their principal explicitly. No password is embedded. The
managed Windows tasks use `InteractiveToken` and require that user to be signed in; they do not
run while you are signed out. Definition generation and inspection do not contact Exchange or
change native tasks. Use:

```text
gucmail startup status --profile primary
gucmail startup repair --profile primary
gucmail startup disable --profile primary
gucmail startup uninstall --profile primary
```

Every mutating startup command provides a preview and requires explicit application/confirmation.
Do not hand-copy a service definition from another account because its executable path and state
scope are deliberately account-specific.

## Operations and recovery

The watcher publishes a mailbox-content-free heartbeat and accepts cooperative controls:

```text
gucmail headless pause --profile primary
gucmail headless resume --profile primary
gucmail headless stop --profile primary
```

`stop` never force-terminates a process. A stale heartbeat is reported by `headless health`; inspect
the redacted logs and supervisor status before restarting. The single-instance lease prevents two
watchers for the same scope from racing. A crashed lease expires and is recovered without discarding
the last successful checkpoint.

For scheduled mail, run `gucmail scheduler run-once --max-jobs 20` as a separate supervisor job.
Watcher and scheduler state share transaction-safe SQLite storage but remain distinct processes.

## Security checklist

- Run under a dedicated ordinary user, never an administrator/root account merely for convenience.
- Restrict the config, data, log, and watcher-state directories to that user.
- Keep command hooks disabled unless a fixed executable and fixed arguments have been reviewed.
- Keep the poll interval at least 60 seconds and prefer the default 300-second interval.
- Never put a password or webhook URL in the unit, task arguments, environment file, or logs.
- Use `doctor`, `headless validate`, `headless health`, and redacted diagnostics before changing retry
  or polling policy.

See [Monitoring and notifications](index.md) for foreground `watch` and
[Windows Task Scheduler](../scheduling/windows-task-scheduler.md) for the scheduled-send worker.
