# Scheduled mail and Undo Send

Write a message now and have gucmail send it later. Scheduled mail waits on your computer, not on
the GUC server, so your computer must be on, awake and online at the send time. The app itself
doesn't need to be open.

## Who sends it

If new-mail alerts are on (`gucmail notifications on`), their background task also sends
scheduled mail. It checks every 30 seconds.

Without alerts, install the scheduler task once. Run it without `--apply` first to see what it
will set up.

```text
gucmail scheduler install
gucmail scheduler install --apply
```

On Windows this adds a task named **GUC Mail Scheduler** that runs every minute and catches up
after sleep. `gucmail scheduler uninstall --apply` removes it. Your scheduled mail stays.

## Schedule a message

In the app, write the message and choose **Send later**.

From the terminal, use an exact time or a delay.

```text
gucmail send --to friend@example.test --subject "Notes" --body "See attached" --at "2026-08-06T09:00:00+03:00"
gucmail send --to friend@example.test --subject "Notes" --body "See attached" --in "45 minutes"
gucmail drafts schedule DRAFT_REFERENCE --at "tomorrow 10:00"
```

gucmail shows the exact date, time and time zone before saving. It won't guess at unclear dates
like `03/04/2027`. Times use your time zone, `Africa/Cairo` by default.

## See and change what's waiting

```text
gucmail scheduled list
gucmail scheduled show SCHEDULE_REFERENCE
gucmail scheduled edit SCHEDULE_REFERENCE
gucmail scheduled reschedule SCHEDULE_REFERENCE --at "2026-08-10T09:30:00+03:00"
gucmail scheduled cancel SCHEDULE_REFERENCE
gucmail scheduled send-now SCHEDULE_REFERENCE
```

In the app, **Ctrl+K → Open Scheduled** shows the same list.

## Undo Send

Undo Send holds a message for a few seconds after you press Send, so you can take it back. Set
**Undo Send duration** in Settings to turn it on. It's off by default.

```text
gucmail scheduled undo SCHEDULE_REFERENCE
```

Once the countdown ends and the server has the message, it can't be taken back. Undo Send is not
the same as Outlook's recall.

## What happens if…

| Situation | What gucmail does |
|---|---|
| Your computer is asleep | Sends when it wakes. History shows it went late |
| You're offline | Waits and tries again. Nothing is lost |
| Your password changed | Waits until you sign in again |
| An attachment changed or disappeared | Stops and asks you to check the message |
| The connection drops mid-send | Marks it **uncertain** and doesn't send again by itself |

For an **uncertain** message, look in **Sent** first. If it's there, it went. If not, send it
again yourself. gucmail never sends twice on its own.

Daylight-saving gaps and repeats are flagged before scheduling. Adding a UTC offset, like
`+03:00`, avoids them.

## Repeating messages

Off until you create one. Each repeat is its own message with its own history.

```text
gucmail recurring create
gucmail recurring list
gucmail recurring pause RECURRING_REFERENCE
gucmail recurring skip RECURRING_REFERENCE
gucmail recurring resume RECURRING_REFERENCE
```

Repeats can be daily, weekly, monthly or on chosen weekdays, with an end date or a count.

## Check that it works

```text
gucmail scheduler status
gucmail scheduler doctor
gucmail send --to friend@example.test --subject "Test" --body "Hi" --in "5 minutes" --dry-run
```

`--dry-run` checks the message without saving or sending it.

## Privacy

Scheduled mail, with its recipients and attachments, is stored in your gucmail data folder until
it's sent. Logs never include message text or passwords.
