# Start monitoring with Windows

Automatic startup runs the headless new-mail watcher after sign-in. The TUI does not need to remain open. Your PC must still be awake, connected, and able to unlock the configured credentials. Delayed-send scheduling uses a separate worker definition and should not be confused with mailbox monitoring.

## Easy path in Settings

1. Open **Settings**, find **Start at login**, and choose **Open setup**.
2. Choose **Enable** in **Windows Startup**.
3. Select a monitoring profile and keep the recommended startup delay.
4. Review the generated executable, arguments, working directory, retry policy, and output location.
5. Confirm the local-system change.
6. Use **Refresh/Test status** to inspect the managed task. Then open Activity or Watcher Status and confirm that one instance owns the profile.
7. Run a synthetic desktop-notification test.

The application creates a managed Windows Task Scheduler entry. It contains an absolute executable and fixed argument list, not a password. The task waits briefly after login, starts only one watcher for the profile, can wait for network availability, restarts with bounded delay, and writes bounded status/log output.

The login trigger and task principal are bound to the current Windows user's SID, read from the
running process's Windows token. Run setup from the intended ordinary Windows account. The task
uses an interactive token, so it runs while that user is signed in; logged-out execution is not
provided by this setup. A locked session, sleep/resume, visible toast delivery and scheduled sends
need their own operational checks. An inspected or generated definition alone does not prove them.

## PowerShell and Command Prompt

Run commands from an activated environment or use the installed executable's full path. These forms work in PowerShell and Command Prompt:

```text
gucmail startup enable
gucmail startup enable --apply
gucmail startup status
gucmail startup repair
gucmail startup disable
```

`startup enable` is preview-only until `--apply` and the normal exact confirmation are supplied. `startup status` compares actual task state with the expected managed definition. `startup repair` previews corrections for a moved executable, changed arguments, missing output directory, or drifted task settings. `startup disable` stops automatic launch but does not delete mail, notification history, profiles, or credentials. Use `startup uninstall` only when you intend to remove the managed definition completely.

## Verify and troubleshoot

After enabling startup, sign out and in only if a real end-to-end check is necessary. Usually these bounded checks are enough:

```text
gucmail startup status
gucmail headless status
gucmail headless health
gucmail notifications test
```

If status says the task exists but its target is missing, preview repair. If the watcher reports authentication required after a password change, update the one keyring entry and restart once. If it says another watcher is active, inspect that owner rather than deleting its lock.

If inspection is denied, times out, or returns an unreadable definition, the startup state is
**Unknown**. Resolve the reported error and refresh before enabling, repairing, disabling, or
removing the integration. An error does not mean the task is absent. An unrecognized localized
missing-task response also remains Unknown. If the scheduler refuses removal, the application
retains its local definition and reports the error. Refresh status before retrying because an
earlier step may have succeeded. On systemd, reloading follows local definition removal; a
failed reload is reported as a partial change. Both the CLI and Settings recheck the reviewed
state after confirmation.

Sleep pauses local detection. The managed task can resume after wake, perform a bounded catch-up, and keep duplicate suppression. Notification latency under polling remains up to the configured interval. For server operation, use [Run monitoring on an always-on computer](help:server.headless). For symptoms and safe diagnostic fields, use [Notification troubleshooting](help:troubleshooting.notifications).

The generated command retains the selected configuration file and data/cache/log directories.
A custom `--config` file or data directory therefore continues to work after sign-in. Refresh/Test
status inspects the task; it does not send a notification or create an Exchange session. Disable,
Repair, and Uninstall also show a concrete preview and require confirmation.
