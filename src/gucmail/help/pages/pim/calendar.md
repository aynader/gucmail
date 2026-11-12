# Calendar, meetings, and availability

Calendar views use bounded date windows and retain original event time-zone information while rendering in the configured display zone. All-day events remain distinct from midnight-to-midnight timed events.

## Everyday workflow

Agenda, day, work-week, week, month, search, and detail views are read-only until you explicitly choose a mutation. Creating an appointment has no attendees; adding attendees creates a meeting workflow that may send invitations. Update plans list exact fields so omitted properties from a partial fetch are not erased.

Responding, cancelling, or notifying attendees requires a preview. For a recurring event, choose
**This occurrence** or **Entire series** before editing or deleting. Entire-series editing loads
the real series start. An occurrence keeps its own reference and cannot change the repetition
pattern. Review the scope, subject and times before confirming; Escape cancels without saving.
Supported repetition patterns use typed Exchange mappings. **This and future** and complete
exception/deleted-occurrence history remain unavailable. Ambiguous or nonexistent local times
need resolution before saving.

## View, week start and display zone

Settings → Calendar controls the default view (agenda, day, work week, week or month),
first weekday (Saturday by default, Sunday or Monday), and display zone (`Africa/Cairo`
by default). Bare `gucmail calendar` opens that saved view; explicit subcommands override
it for one invocation. Changing the default affects newly opened Calendar workspaces;
the current selector keeps your choice. Week and month use the same week alignment.
Month includes adjacent-month dates in complete weeks, using one bounded query.

Default dates and Today follow the display zone. Timed events show local offsets;
all-day dates retain the original event zone and exclusive end date. Complete details
retain stored timestamps and the event zone. JSON/export and existing-item edit forms
keep those original values. Apply updates mounted presentation locally; refresh queries
with the newly captured week start and zone. An open availability form keeps its captured
zone and working policy. No preference change rewrites an event.

## Working days and hours

Settings → Calendar provides **Working days**, **Working-hour start** and **Working-hour end**.
Use full weekday names separated by commas, and local 24-hour `HH:MM` times. The editable
defaults are Monday–Friday, `09:00`–`17:00`; they do not describe a verified GUC timetable.
End must follow start on the same day. Stage both endpoints and Apply together when needed.

Choose **Work week** to list events overlapping your working days. Full week and month retain
every day; event views retain all hours. One bounded seven-day query is filtered locally, so a
result limit may leave fewer working-day events visible. All-day and overnight overlaps are
preserved without changing the original item.

**Find free time** uses the saved schedule and requires the whole meeting inside one local
working window. Clear **Use working days/hours** to search all hours in the explicit window.
The form captures its policy when opened; new forms and refreshes read applied Settings.
Ambiguous or nonexistent DST boundaries need different working hours or an explicit all-hours
window. Missing or incomplete attendee data is never considered free. Cancel/Check remain
visible while fields scroll in smaller terminals. This operation creates no meeting.

CLI `calendar free-time --attendee <address> --start <offset-time> --end <offset-time>` uses
the same policy. Choose `--duration-minutes` and `--interval`, or add `--all-hours` to override
saved working time. One lookup is bounded to 100 unique attendees and 31 elapsed days.

## Capability boundaries

Settings → Calendar also offers **Initial invitation response**, **Calendar event colors**,
**Calendar change notifications** and **Related-mail searches**. The response choice seeds a
fresh ordinary form or omitted CLI response argument; explicit Accept/Tentative/Decline wins.
It never responds automatically. Review and final confirmation remain required, and an already
open form keeps its selected response.

Colors affect TUI subjects only: None, Availability, or the first nonempty Category. Availability
uses Free green, Working elsewhere cyan, Tentative yellow, Busy blue and Out of office magenta.
Category colors are stable labels. Text details and saved fields/JSON/ICS remain unchanged.
Apply recolors an open Calendar without a query. Related mail (`h`) remains an explicit bounded
subject search: at most five events and 100 messages, with possible unrelated subject matches.
Disable blocks queries and late results; it does not erase an already opened mail view.

Calendar change notifications governs stored Calendar events in configured ordinary watchers,
including queued quiet-hour/digest entries. Off skips pending Calendar deliveries and excludes
their contents from mixed mail digests. Suppressed entries do not replay when enabled again.
It does not choose a provider, start monitoring, hide structured reports or schedule expanded
occurrence reminders. `calendar watch` remains provider-free reporting. Existing notification
rules/privacy/retries still apply; delivery already started cannot be undone.

Settings → Calendar controls the duration, reminder and availability of new events. A timed
event starts with 60 minutes, no reminder and Busy unless changed. Form edits override these
defaults. Applying Settings affects the next new form, including creation from a message;
existing events and open forms keep their values. Timed durations count elapsed minutes across
daylight-saving transitions; an all-day event ends at the next local midnight by default.

`calendar create --start <time> --dry-run` previews the saved defaults without authentication.
Use `--end` or `--duration-minutes` to choose its length, `--reminder-minutes` or `--no-reminder`
to control the reminder, and `--availability` to override the initial state. Actual creation
requires confirmation; attendees may receive invitations under the explicit invitation policy.

`calendar watch` reports native event/series changes from one bounded metadata page, with a quiet
baseline that resumes across invocations. Add `--forever` to keep checking at the normal interval.
This is stored-item change detection, not a reminder for each expanded recurrence occurrence.

Free/busy, rooms, recurrence details, reminders, and invitation behavior depend on Exchange version and account permission. An unavailable result is not a failed write. ICS import first creates a bounded plan; export files can contain private attendees, links, locations, and notes. Backups can preserve supported fields for offline reading but cannot respond to meetings offline.


## Series occurrence history

`calendar history EVENT_REFERENCE` and Calendar → Series history explicitly read current
first/last, modified and deleted occurrence metadata without fetching bodies or attachments.
The selected occurrence keeps its reference while its master is resolved. The default limit
is 100 per modified/deleted list, up to 200 each; truncation and missing fields are explicit.
EWS may return a larger metadata field before local truncation. This is current server state,
not a complete edit audit or expanded occurrence schedule. JSON preserves original timestamps;
the TUI/human view uses the display zone. Read-only history cannot be restored by ICS or normal
backups, which do not include this separately requested snapshot.
