# Calendar

## Quick explanation

Calendar is a first-class view over Exchange appointments and meetings. It supports bounded agenda,
day, work-week, week, month, and upcoming views, plus create, edit, delete, meeting responses,
conflict checks, and ICS import/export through shared service contracts.

Server version, permissions, folders, rooms, and free/busy access are not assumed; a refused
operation is reported as unsupported or unauthorized.

## View events

```text
gucmail calendar agenda
gucmail calendar day 2026-08-06
gucmail calendar week 2026-08-06
gucmail calendar month 2026-08-01
gucmail calendar search "synthetic workshop"
gucmail calendar show EVENT_REFERENCE
```

Views use bounded date windows and result limits. The TUI displays the configured zone while
retaining the original event zone. All-day events are distinct from midnight-to-midnight timed
events.

## Create or edit an event

```text
gucmail calendar create --subject "Workshop" --start 2026-08-06T14:00:00+03:00 --end 2026-08-06T15:00:00+03:00
gucmail calendar edit EVENT_REFERENCE --location "C7.101"
```

Enter a subject, start, end, time zone, location, availability, reminder, categories, body, and
optional attendees. Review the resolved absolute times before confirmation. An appointment has no
attendees; adding attendees creates a meeting workflow and may send invitations.

Editing a recurrence requires choosing an occurrence or the entire series. A change plan lists the
exact fields to update so omitted data is not accidentally erased.

The TUI asks for scope before opening a recurring event's editor or deletion preview. An
entire-series edit loads the actual recurring master; a single occurrence keeps its own reference
and cannot change the series repetition pattern. Saving recurrence changes and deleting or
cancelling a series require a preview and confirmation. Escape cancels without writing.

## Meeting invitations

Pending invitations can appear alongside inbox actions. Review organizer, attendees, original time
zone, conflicts, location, and notes before responding:

```text
gucmail calendar respond EVENT_REFERENCE accept
gucmail calendar respond EVENT_REFERENCE tentative
gucmail calendar respond EVENT_REFERENCE decline
```

Settings → Calendar provides **Initial invitation response**: Accept, Tentative or Decline.
It seeds the ordinary TUI response form and `calendar respond EVENT_REFERENCE` when the response
argument is omitted. An explicit argument or palette Accept/Tentative/Decline action wins.
This preference never responds automatically. The form still opens for review, and sending
requires a separate confirmation. Existing events and already open response forms keep their values.

Sending a response is mutating. Where the server allows it, the client distinguishes updating the
calendar without a response from sending a response. A non-decline response may include both
`--proposed-start` and `--proposed-end`; server acceptance of proposed times is capability-gated.

Deleting an appointment and cancelling a meeting are not the same. Cancellation can notify
attendees and requires a preview. Permanent series deletion is destructive.

## Time zones and recurrence

The default display zone is `Africa/Cairo`. The service validates UTC conversion, original event
zone, ambiguous local times, nonexistent local times, daylight-saving transitions, recurrence
patterns and recurrence boundaries. Occurrence and entire-series edits have distinct identities;
this-and-future splitting and complete exception/deleted-occurrence history remain unavailable.
Use an offset-qualified value such as
`2026-08-06T14:00:00+03:00` in automation.

## Availability and conflicts

```text
gucmail calendar availability --attendee colleague@example.edu --start "2026-08-06T09:00:00+03:00" --end "2026-08-06T17:00:00+03:00"
gucmail calendar conflicts --days 1
```

Free/busy and room information depend on `GetUserAvailability`, `GetRoomLists`, and account
permission. An unauthorized result is reported explicitly; the client does not infer another
person's availability.

## Import, export, backup, and offline use

```text
gucmail calendar import example.ics
gucmail calendar export EVENT_REFERENCE --output example.ics
```

Import parses a bounded ICS file and produces a plan before creating server items. Export is local
and read-only after the event is fetched; replacing an existing file requires `--overwrite` and
confirmation. `calendar create` and `calendar edit` accept repeatable `--attach` paths, and edit can
remove known attachments with `--remove-attachment`. Calendar items included in a backup can be browsed offline,
but offline mode cannot accept invitations or change Exchange.

## Common mistakes and recovery

Use `calendar watch` for one bounded page of native event changes, or `calendar watch --forever`
for conservative periodic checks. The initial metadata baseline is quiet and resumable. Later
cycles report stored-event or series creations, updates and deletions. This does not expand
recurring occurrences into reminders or deliver notifications externally. See
[Monitoring](../monitoring/index.md) for explicit notification routing and checkpoint recovery.

- A time is one hour wrong: compare configured, original, and explicit-offset zones.
- A whole series changed: choose occurrence scope for a single exception.
- Attendees were notified unexpectedly: inspect the invitation policy before confirmation.
- A server says unsupported: inspect the calendar family in `gucmail ews capabilities`.

## Security and privacy

Calendar bodies, attendees, rooms, and meeting links are private mailbox data. They are excluded from
normal logs. ICS files may contain private details and should be handled like exported mail.

## Read-only series occurrence history

Use `gucmail calendar history EVENT_REFERENCE` or Calendar → **Series history** to inspect
current first/last, modified and deleted occurrence metadata. The TUI runs the request in
a cancellable worker and discards its result if the highlighted event changes. An occurrence
or exception resolves its real master without replacing the selected event's local identity.
The adapter requests only occurrence metadata, with no bodies, attendees or attachments, and
does not enumerate CalendarView or modify a meeting. Ordinary Calendar list requests are unchanged.

`--limit` defaults to 100 and accepts 1–200 entries **per modified/deleted list**. Returned
metadata stops at that limit and reports truncation; the server may return a larger metadata
field before local truncation because EWS does not paginate these fields. First/last records
are separate. A missing field is **unavailable**, not evidence of empty history; an explicitly
empty list is reported separately. JSON retains original timestamps and short local item
references, while human/TUI timestamps use the display zone.

This is the server's current exception/deletion state, not a history of every previous edit
or a schedule of all occurrences. Deleted entries may contain only a start time. Permissions,
missing/stale masters and unsupported metadata are reported without pretending success.
Read-only server occurrence fields cannot be restored as writable history. Standard backups
and ICS exports do not include this separately requested snapshot; redirected JSON can retain
the metadata locally but does not provide lossless Calendar restore.

## Presentation and related mail

**Calendar event colors** affects TUI subjects only: None initially, Availability, or Category.
Availability uses Free green, Working elsewhere cyan, Tentative yellow, Busy blue and Out of office
magenta. Category uses a stable palette color for the first nonempty category; no category means
no color. Category colors are labels, not importance scores. Event details retain textual
availability/categories, and event records, JSON and ICS exports keep their original fields.
Apply changes an open Calendar locally without another Exchange request.
The message list puts the subject before full start/end timestamps so it stays visible in
compact terminals. Scroll horizontally or open details to read the complete timestamps.

**Related-mail searches** controls the Related mail action and its `h` shortcut. Off blocks the
action before connection, stops further queries after a pending read, and discards late results.
On permits an explicit server-side subject search, at most five selected events and 100 total
message results. Subject matches suggest related mail; they do not prove invitation or conversation
identity. The preference does not erase messages or already opened mail views.

## Calendar change notification preference

**Calendar change notifications** allows or suppresses stored Calendar change events delivered by
configured ordinary watchers. It defaults On, preserving existing provider policies. For example,
`gucmail watch --folder calendar --strategy incremental_sync --no-webhooks` uses the existing
console destination; configured profile routes can use other explicitly configured providers.
Turning this preference Off also suppresses pending quiet-hour/digest entries, with safe skipped
outcomes, and removes Calendar members from mixed digests while retaining allowed mail members.
Notification privacy, rules, retry history and provider selection still apply. Suppressed queued
events are resolved and do not reappear simply because the preference is enabled later.

The control does not start a watcher, select a provider, alter polling cadence or schedule reminders
for expanded recurrence occurrences. `calendar watch` remains provider-free change reporting;
it does not consume another watcher's deliveries. Turning delivery Off does not hide structured
change reports or delete local event metadata. Delivery already started cannot be undone.

## Read a saved backup offline

In offline mode, open this area and select an explicit completed local backup. The screen shows
its source date and warns that the saved copy can be stale. Search covers this area's saved
records across the selected backup before paginating matches. It runs in a cancellable worker;
changing the query returns to the first result page. Encrypted backups require the passphrase.
This view does not contact Exchange or offer server mutations. Attachment contents are not
read for this search; it covers saved fields and attachment metadata.

Supported recurrence patterns use typed EWS XML for create, edit, and read. Calendar boundary
instants are interpreted in the event's time zone, preserving the local start/end dates across
UTC offsets and daylight-saving changes. Occurrence and entire-series scopes are distinct;
"this and future" edits and a complete historical exception/deletion audit remain unavailable.
The explicit Series history view exposes bounded current server metadata as described above.
Some recurrence patterns, meeting policies, and actions may still be limited by the server or by
your account permissions.
