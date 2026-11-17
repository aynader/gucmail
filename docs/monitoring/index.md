# New-mail alerts

gucmail can tell you when new mail arrives. It's off until you turn it on.

## Turn alerts on

```text
gucmail notifications on
```

This sets up Windows notifications, starts a background task that checks for mail, and sends a
test alert. Each step shows ✓ or ✗. The task starts with Windows, so alerts keep coming after a
restart. It also sends your [scheduled mail](../scheduling/index.md).

```text
gucmail notifications status
gucmail notifications off
```

In the app, the header shows **Alerts on** while the background task runs.

The first check only notes what's already in your inbox, so you aren't alerted about old mail.

## Watch from a terminal instead

```text
gucmail watch --folder inbox
gucmail watch --folder inbox --forever
gucmail watch --folder inbox --desktop
```

Without `--forever` it checks once and exits. With it, it keeps checking until you press Ctrl+C.

`--strategy incremental_sync` asks the server for every change, including deletions and mail
marked read elsewhere.

```text
gucmail watch --strategy incremental_sync --folder inbox --forever
```

If it reports an expired checkpoint, add `--reset-checkpoint` once.

The GUC server doesn't push mail to gucmail. gucmail checks on a schedule, with a minimum of 60
seconds, and waits longer when the server is busy.

## Where alerts go

| Destination | How |
|---|---|
| Windows notifications | `gucmail notifications on` |
| The terminal | Always on when you use `gucmail watch` |
| Discord or your own web service | [Integrations](../integrations/index.md) |
| A file, one event per line | `--event-log alerts.ndjson` |
| Your own program | `--command-hook` (asks first) |

Webhooks you added also receive alerts. `--no-webhooks` keeps a run on your computer.

## Choose which mail alerts you

**Notifications → Rules** in the app lets you alert only for some senders, folders or subjects, set
quiet hours and get daily digests. **Simulate** tries your rules on sample mail without sending
anything.

```text
gucmail notifications rules test RULE_ID --profile PROFILE
gucmail notifications history
```

`history` lists recent alerts and whether they arrived.

## Run on an always-on computer

See [running alerts on a server](headless-deployment.md).
