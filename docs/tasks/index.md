# Tasks

## Quick explanation

Tasks are Exchange items with a subject, body, start/due dates, status, completion percentage,
importance, categories, reminders, and optional recurrence. They are available only when the server
exposes a task folder and permits the operation.

## Common workflow

```text
gucmail tasks list
gucmail tasks search "synthetic follow-up"
gucmail tasks show TASK_REFERENCE
gucmail tasks create --subject "Submit report" --due 2026-08-26
gucmail tasks create --subject "Call advisor" --reminder-at 2026-08-24T09:00:00+02:00
gucmail tasks edit TASK_REFERENCE --due 2026-08-27
gucmail tasks edit TASK_REFERENCE --clear-reminder
gucmail tasks complete TASK_REFERENCE
gucmail tasks reopen TASK_REFERENCE
```

The supported status vocabulary is `not_started`, `in_progress`, `completed`,
`waiting_on_others`, and `deferred`. Completion sets a consistent completed state and 100 percent;
reopening requires a non-completed state and updated percentage. Update plans list explicit fields
so a partially loaded item does not erase omitted data.

## Recurrence and reminders

Settings → Tasks can seed a fresh task with a daily, weekly, monthly or yearly fixed-date
repeat, or a number of days, weeks, months or years after completion.
The default is None. Interval means days, weeks or months (yearly requires one); an optional
occurrence limit includes the first occurrence. Blank means no end. The rule uses the supplied
start date, otherwise the due date, otherwise today's date in the configured time zone.
Without a start or due date, a repeating task receives that local date as its start.
Weekly repeats use the anchor weekday; monthly and yearly repeats use its day/month.
Existing tasks, imported records and already-open forms retain their own recurrence.

```text
gucmail config set tasks.default_repeat weekly
gucmail config set tasks.default_repeat_interval 2
gucmail config set tasks.default_repeat_count 3
gucmail tasks create --subject "Review report" --start 2026-10-04 --dry-run
gucmail tasks create --subject "Review report" --repeat daily --repeat-interval 5 --repeat-forever --dry-run
gucmail tasks create --subject "One-off task" --repeat none --dry-run
```

`--repeat-count`, `--repeat-forever` and `--repeat-until YYYY-MM-DD` are alternative end choices.
Explicit CLI options override the saved policy for one invocation. Dry-run does not authenticate;
actual creation requires confirmation. The normal form offers the same ordinary patterns;
advanced existing rules keep their validated JSON instead of being simplified silently.

Fixed-date repeats use the start date. The `after_completion_daily`,
`after_completion_weekly`, `after_completion_monthly` and `after_completion_yearly`
choices use Exchange's distinct regeneration elements: the next task is due the chosen
number of periods after completion. The client allows intervals from 1 to 999 for these
choices; fixed-date weekly/monthly retain their 99 limit, and fixed-date yearly needs 1.
Exchange manages the next occurrence, count and end boundary; the client does not calculate
future completion dates or start a local scheduler. See Microsoft's
[TaskRecurrenceType reference](https://learn.microsoft.com/en-us/exchange/client-developer/web-service-reference/recurrence-taskrecurrencetype).
The CLI and ordinary Repeat chooser expose these choices. They retain the task-only
`task_regeneration` pattern, unit, interval and boundary in JSON and task archives, and
restore them through the native EWS serializer. A fixed-date RFC RRULE cannot retain
completion-relative meaning and conversion fails explicitly. Existing tasks and open forms
keep their original rules. Occurrence editing and complete recurrence history remain partial;
server regeneration and restored-task behavior are not live-verified.

```powershell
gucmail tasks create --subject "Review after completion" --repeat after_completion_weekly --repeat-interval 2 --repeat-count 4 --dry-run
```

When completing a recurring task, Exchange can return a new item ID for the completed instance
while keeping the original ID for the next occurrence. The command returns the completed
instance's separate local reference; the original reference continues to identify the series.
Refreshing or reopening local state preserves both identities. A final or non-recurring task
can keep the same ID. This also applies when Exchange creates a one-off instance after editing
a fixed-date recurring task's dates. A failed read after an accepted update is not a reason to
repeat completion: inspect the original series and the returned instance first. These identity
paths use synthetic native EWS types and responses, not a live server lifecycle. See the
[Task UpdateItem reference](https://learn.microsoft.com/en-us/exchange/client-developer/web-service-reference/updateitem-operation-task).

## Local observed history

`tasks history TASK_REFERENCE` reads accepted updates observed by this application without
credentials or an Exchange connection. JSON includes `observations` and `next_before`;
continue with `--before SEQUENCE --limit 20`. In a loaded Tasks workspace, **Local history**
shows the same records in the scrollable detail pane. **Older history** and **Newest history**
navigate retained updates without an additional mailbox request.
The command palette's **Show local Task history** uses the highlighted Task. Its configured
shortcut remains available while the Tasks search field is focused.

The record contains UTC observation time, edit/complete/reopen action, original and returned
local references, whether a separate instance was returned, and result-readback status.
It contains no subject, body, categories, recipient addresses, raw EWS IDs or change keys.
Links follow actual returned identities; matching names, dates or recurrence rules do not
create relationships. Ordinary edits can report `readback_not_requested`; this is not fresh
server verification. A failed or interrupted post-update read can leave an unavailable or
pending accepted result. Inspect current state before deciding whether to repeat it.

State schema 11 preserves at most 10,000 observations per account and supports restart and
concurrent writers. Clearing disposable cache retains the identity mappings needed by these
links. The provider's cacheless API keeps a bounded session-only record, explicitly unavailable
after exit; the CLI reads durable local state. Creation, deletion, unseen past activity and
complete server recurrence history are not reconstructed. This journal is not a mailbox backup
and does not provide lossless history restore. Older retained relationships can expire.

Missing update identities, mismatched/native-wrong/multiple Task readback responses, unusable
result data or local recording failures after an accepted update produce an uncertain result,
without repeating the write. Refresh the bounded Tasks list for review.

Recurring tasks use server recurrence where supported. Review the pattern, start, boundary, and
whether an action affects one occurrence or the series. Task reminders use an absolute ISO-8601
time with an explicit UTC offset; the client normalizes it to UTC and persists Exchange's
`reminder_due_by` plus `reminder_is_set` fields. Use `--clear-reminder` to disable one. Local message
reminders are a different feature.

Typed simple recurrence patterns can be created, read, edited at series level, and restored from
backup. Occurrence-scoped task editing and complete recurrence history are still unavailable;
the presence of a scope value does not establish those workflows.

## Delete, export, backup, and offline use

```text
gucmail tasks delete TASK_REFERENCE
gucmail tasks export --output tasks.json --format json
```

Ordinary Delete moves the stored task, including its recurrence, to Deleted Items. It does
not skip the current occurrence. `--permanent` explicitly removes the specified occurrence
with no Deleted Items copy. `--permanent --series` removes the active recurring series;
execution also requires `--confirm-series TASK_REFERENCE` with the exact reference and
ordinary confirmation (`--yes` for non-interactive use). A separate completed instance is
not an active series. The adapter verifies recurrence with a bounded metadata-only read
before series deletion and fails without writing when that scope cannot be established.
The response must be a Task with the same item ID and an explicitly true recurring flag.
A missing or false flag, even alongside recurrence data, requires refreshing or choosing
occurrence deletion explicitly; it never silently changes the requested scope.

```text
gucmail --json --non-interactive tasks complete TASK_REFERENCE --dry-run
gucmail --json --non-interactive tasks reopen TASK_REFERENCE --dry-run
gucmail --json --non-interactive tasks delete TASK_REFERENCE --permanent --series --dry-run
```

Completion, reopening and deletion previews do not construct application state or resolve
credentials. A preview is a local plan, not verification of the current server item.
The TUI's ordinary **Delete** retains its recoverable move and confirmation. **Delete options**
offers the three explicit modes; permanent modes require `DELETE OCCURRENCE` or `DELETE SERIES`
and a final confirmation. Changing the choice clears the phrase. Permanent batches stop
after a failure or cancellation and report completed, failed/uncertain and skipped references.
Refresh current state before deciding to repeat an uncertain action. Deletion uses Exchange's
`AffectedTaskOccurrences` semantics; how the server regenerates a recurring task after deletion
can vary, so refresh to see the result.
See Microsoft's [DeleteItem reference](https://learn.microsoft.com/en-us/exchange/client-developer/web-service-reference/deleteitem).

Exports and backups preserve fields that the format can represent. Offline task browsing and search
are read-only; completing or editing requires the live server.

## Cross-feature use

A message can become a follow-up task, and a task can link back to its source message through a
local reference. The source message is not moved or marked read by creating the task. A calendar
event suggestion remains a draft until confirmed.

In the TUI, open the command palette and run **Create task from message** while a message is
selected. The real task editor opens with a bounded subject, the stable local source reference, and
the `From message` category; nothing is created until the form is saved. **Add reminder**, **Change
due date**, and **Change priority** open the selected task directly at the relevant editor field.

## Troubleshooting and safety

- If a task reference becomes stale after update, list the task folder again.
- If recurrence is unsupported, use separate local follow-up entries or nonrecurring server tasks.
- If assignment metadata is unavailable, the client shows the supported personal-task fields only.
- Task bodies and categories are private and excluded from normal logs.

See the [EWS capability matrix](../ews/index.md) for tested implementation paths and unprobed server
status.

## Read a saved backup offline

In offline mode, open this area and select an explicit completed local backup. The screen shows
its source date and warns that the saved copy can be stale. Search covers this area's saved
records across the selected backup before paginating matches. It runs in a cancellable worker;
changing the query returns to the first result page. Encrypted backups require the passphrase.
This view does not contact Exchange or offer server mutations. Attachment contents are not
read for this search; it covers saved fields and attachment metadata.

Task text, category, due-date, and completion filters are sent to Exchange before applying the
result limit. Today includes today's due dates; This Week covers today and the following six
calendar days. The Open/Due views exclude completed tasks at the server. Individual incomplete
status values (for example In Progress versus Not Started) require a bounded local residual
filter because the installed EWS field definition does not permit status restrictions. That
residual examines at most 200 server candidates per page. Next/Previous navigate candidate
windows in the Tasks screen. CLI `tasks list` and `tasks search` accept `--offset`; `--page`
includes `items`, `scanned`, `offset`, and `next_offset` in the result. Continue with the returned
offset even when a page has no matches. A full final window can offer one extra empty page.
Default results remain arrays with continuation hints in warnings. Offsets count examined
candidates, not matches, and concurrent mailbox changes may require refreshing from zero.

Exchange owns the completion timestamp. Completing a task writes its status and percentage, then reads the server-assigned completion time. Reopening clears completion state without attempting to write the read-only EWS CompleteDate field.
