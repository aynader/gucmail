# Calendar

Your Outlook calendar, in the app and the terminal. Open it with the **Calendar** tab at the top.

## See your events

```text
gucmail calendar agenda
gucmail calendar day 2026-08-06
gucmail calendar week 2026-08-06
gucmail calendar month 2026-08-01
gucmail calendar search "workshop"
gucmail calendar show EVENT_REFERENCE
```

Times show in your time zone, `Africa/Cairo` by default. Event details also say which zone the
event was set in.

## Add or change an event

```text
gucmail calendar create --subject "Workshop" --start 2026-08-06T14:00:00+03:00 --end 2026-08-06T15:00:00+03:00
gucmail calendar edit EVENT_REFERENCE --location "C7.101"
```

In the app, press `n` for a new event or `e` to edit. You see the exact times before saving.

Adding attendees turns an event into a meeting and sends them invitations. gucmail asks first.

For a repeating event, gucmail asks whether you mean this one or the whole series.

`--attach` adds a file to an event. `--remove-attachment` removes one.

## Reply to invitations

```text
gucmail calendar respond EVENT_REFERENCE accept
gucmail calendar respond EVENT_REFERENCE tentative
gucmail calendar respond EVENT_REFERENCE decline
```

You can propose another time with `--proposed-start` and `--proposed-end`. gucmail never replies for
you. **Initial invitation response** in Settings only picks which answer is selected first.

Deleting your own appointment and cancelling a meeting are different. Cancelling tells the
attendees. gucmail shows who will be told before you confirm.

## Who's free

```text
gucmail calendar availability --attendee colleague@example.edu --start "2026-08-06T09:00:00+03:00" --end "2026-08-06T17:00:00+03:00"
gucmail calendar conflicts --days 1
```

Free/busy needs the GUC server to share it. If it won't, gucmail says so instead of guessing.

## Import and export

```text
gucmail calendar import invite.ics
gucmail calendar export EVENT_REFERENCE --output event.ics
```

Import shows what it will add before adding anything. Export won't replace a file unless you add
`--overwrite`.

## Changes and history

`gucmail calendar watch` reports events that were added, changed or removed since last time.
`gucmail calendar history EVENT_REFERENCE` shows which occurrences of a repeating event were moved
or deleted.

## Settings

| Setting | Does |
|---|---|
| Calendar event colors | Colour events by availability or category |
| Related-mail searches | Lets `h` find mail with the same subject as an event |
| Calendar change notifications | Alerts for calendar changes from the new-mail watcher |

## Good to know

- If a time looks an hour off, check the time zone in Settings and in the event details.
- "This and all future events" edits aren't supported yet. Change one event or the whole series.
- Calendar items in a backup can be read offline, but not changed.

## Privacy

Event notes, attendees and meeting links are private. They never appear in logs. Treat `.ics`
files like exported mail.
