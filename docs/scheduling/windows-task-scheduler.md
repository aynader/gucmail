# Windows Task Scheduler

GUC Mail CLI sends scheduled messages with a durable local scheduler; it does not rely on a
server-side "send later" feature. The computer must be awake, the user task must be allowed to
run, network access must work, and the keyring credential must remain valid.

## Scheduled sending

### Preview

Run in the same Python environment that owns `gucmail`:

```powershell
gucmail scheduler doctor
gucmail scheduled install
```

The preview shows the absolute Python executable, generated XML path, interval, and native
commands. It performs no install.

### Install

```powershell
gucmail --yes scheduled install --apply --interval 60
gucmail scheduler status
gucmail scheduled run-once
```

The generated task invokes the equivalent of:

```text
ABSOLUTE_PYTHON -m gucmail --quiet scheduler run-once
```

The Task Scheduler definition uses fixed argument arrays; it does not construct a shell command.
Review the generated artifact before applying it.

### Sleep, offline, and password changes

When the computer sleeps, no local send occurs. On the next run, overdue jobs are atomically
claimed and processed under the retry/manual-review policy. Definite temporary failures use bounded
backoff. An ambiguous send moves to manual review rather than being blindly repeated. A changed
password causes authentication failures until the keyring entry is updated with `gucmail login`.

For a message that must be delivered while this computer is off, this local strategy is not
sufficient.

### Pause and remove

```powershell
gucmail scheduler pause
gucmail scheduler resume
gucmail scheduled uninstall
gucmail --yes scheduled uninstall --apply
```

Uninstalling the OS task does not delete queued jobs, drafts, or history. Pausing also retains them.

## New-mail notifications at sign-in

The watcher that delivers new-mail notifications can also start automatically when you sign in.
Each watcher profile gets its own task, named `GUC Mail Watcher (<id>)`, where `<id>` is the
profile ID. The task runs with `pythonw.exe`, so no console window opens.

Preview the plan, then apply it:

```powershell
gucmail startup plan --profile PROFILE_ID
gucmail --yes startup enable --profile PROFILE_ID --apply
gucmail startup status --profile PROFILE_ID
```

`startup status` compares the installed task with the expected definition. To remove it:

```powershell
gucmail startup uninstall --profile PROFILE_ID
gucmail --yes startup uninstall --profile PROFILE_ID --apply
```

Removing the startup task does not delete the watcher profile, its notification history, or other
local state. Only remove tasks whose names start with `GUC Mail`.
