# Tasks, reminders, and completion

Exchange tasks can contain a subject, body, start and due dates, status, completion percentage, importance, categories, reminders, and recurrence. Availability depends on a task folder and account permission.

## Task lifecycle

Settings → Tasks sets the default view, completed-task visibility and page sorting.
New Tasks workspaces and `tasks list` use those saved choices. An open workspace retains
its selected view; changing the default affects the next workspace. All, Today and This week
honor completed visibility. Open, Due (today or earlier) and Overdue exclude completed tasks;
Completed explicitly shows them. Today and the next seven dates use the configured time zone,
so midnight follows that zone rather than the computer's date.

Sorting offers due date, subject, importance (high first) and status. It sorts each bounded
candidate page; it does not fetch the full task folder to guarantee global ordering. Undated
tasks follow dated tasks in due-date order, and completed tasks follow incomplete tasks.
Apply updates the loaded candidates locally, preserving still-visible selections and full
details. Refresh explicitly to fetch a window with the new visibility or date restrictions.
Changing the view, visibility or local date restarts continuation at zero. Existing task
dates, reminders, recurrence and edit/export records keep their original values.

`tasks list --view today --sort importance` overrides saved choices for that invocation.
`--completed` / `--no-completed` overrides ordinary visibility. Search uses its explicit
criteria rather than the saved default view; an explicit `--status completed` takes precedence
over hidden completed tasks. `tasks search --overdue` uses the configured local date and
incomplete tasks. These read commands do not change saved preferences or task state.

In the Tasks workspace, Tab reaches the detail pane. Home/End and Page Up/Page Down scroll
complete details; Shift+Tab returns to the preceding control. Compact tables and action rows
can require horizontal scrolling.

Settings → Tasks sets new-item importance, due days and reminder minutes. Defaults are Normal,
no due date and no reminder. Due days follow a supplied start date, or the creation date in the
configured time zone. Reminder minutes follow creation independently of the due date. Blank
disables an optional default; zero means the base date/time. Fresh forms from messages use the
same defaults. Edits of existing tasks and already-open forms keep their values.
Settings shows **No due date** or **Reminder off** for cleared defaults; zero remains enabled.

`tasks create --subject <text> --dry-run` previews locally without authentication. `--due`,
`--reminder-at` and `--importance` override saved defaults; `--no-due` and `--no-reminder` leave
those fields unset. Actual creation requires confirmation. The reminder becomes an Exchange
task property; it does not start a local watcher or notification schedule.

Settings → Tasks also sets new-task repeat pattern, interval and occurrence limit. None is
the default; daily, weekly, monthly and yearly use fixed calendar dates. Weekly/monthly
intervals allow 1–99, daily 1–999, and yearly requires 1. The optional occurrence limit is
1–999 including the first; blank shows **No end**. Stage interdependent changes together.
The supplied start, otherwise due date, otherwise configured local creation date anchors
the repeat. With neither date, a repeating task receives that date as its start. Weekly uses
that weekday; monthly/yearly use its day/month. Fresh forms from messages use the same policy.
Open forms, existing tasks and imported records keep their own rules.

`tasks create --repeat daily --repeat-interval 5 --repeat-count 3 --dry-run` overrides the
saved policy once. Choose count, `--repeat-forever` or `--repeat-until YYYY-MM-DD`; these end
choices are mutually exclusive. `--repeat none` creates a one-off task. The ordinary form
offers the same patterns; complex imported/existing rules remain in the advanced control.
The Repeat chooser and `tasks create --repeat` also offer `after_completion_daily`,
`after_completion_weekly`, `after_completion_monthly` and `after_completion_yearly`.
Exchange makes the next task due the chosen number of periods after completion. These
choices allow intervals 1-999, preserve the native task regeneration pattern in JSON and
task archives, and restore through the typed serializer. They do not calculate future
completion dates locally. Fixed-date RRULE conversion rejects regeneration explicitly.
Occurrence editing and full recurrence history remain partial; actual server behavior is
not live-verified. Existing tasks and open forms retain their original rules.

Completing a recurring task can return a new completed instance while the original item
continues as the next occurrence. The result uses a separate local reference; retain the
original reference to inspect the series. Local state keeps these two identities distinct.
Final/non-recurring tasks can keep the same reference. If a post-update read fails, inspect
the series/returned instance rather than repeating completion automatically.

**Local history** in a loaded Tasks workspace shows accepted edit/complete/reopen observations
without another mailbox request. Scroll the detail pane; **Older history** and **Newest history**
navigate retained records. `tasks history TASK_REFERENCE --limit 20` works without credentials;
use the JSON `next_before` value with `--before` for continuation.
The command palette's **Show local Task history** uses the highlighted Task; the configured
palette shortcut also works while the Tasks search field is focused.
Records contain observation time, action, original/returned local references, separate-instance
identity and readback status. They exclude Task content and raw Exchange identifiers.
`readback_not_requested` means that edit returned local data without a fresh server read.
Durable state keeps at most 10,000 updates per account across restart and cache clearing;
the cacheless provider API explicitly labels its session-only record.
This is observed local activity, not complete server history or a backup: creation, deletion,
unseen past events and lossless history restore remain outside this record.
An unusable result or local recording failure after an accepted operation is uncertain.
Refresh current state before deciding whether to repeat the operation.

Common statuses are not started, in progress, completed, waiting on others, and deferred. Completing a task sets a coherent completed state and full percentage. Reopening requires a non-completed state and revised percentage. Update plans always name non-empty fields so a partially loaded item cannot erase unknown values.

Next and Previous navigate bounded candidate windows. Specific incomplete statuses are matched locally after server text/date/completion/category restrictions. An empty window can still offer Next. CLI list/search accept `--offset`; `--page` includes `items`, `scanned`, and `next_offset`. Continue using the returned candidate offset until null. A full final window may offer an extra empty page. Refresh from zero if concurrent mailbox changes affect the ordering.

Recurring task metadata includes a pattern, start, boundary, and occurrence-versus-series scope. Creation or update remains unavailable unless the adapter can preserve that mapping; an unsupported recurrence plan fails before mutation. A task reminder is a server item property, not the same as a local mail notification rule.

Deletion changes the mailbox and requires confirmation. Export and backup preserve only fields the selected format can represent. Offline task views remain read-only. If an updated reference becomes stale, refresh the task folder once rather than guessing a raw Exchange ID.

Ordinary **Delete** moves the stored task with its recurrence to Deleted Items; it does not
skip an occurrence. **Delete options** separates this move from permanent occurrence or
whole-series deletion. Permanent choices require the exact phrase `DELETE OCCURRENCE` or
`DELETE SERIES`, then a final confirmation; changing scope clears the phrase. Each active
series is verified with a bounded metadata read before deletion. A separate completed
instance is not an active series. A wrong item type or unconfirmed recurring flag stops
deletion; refresh or explicitly choose occurrence deletion. Permanent batches stop on failure or cancellation and
show completed, failed/uncertain and skipped counts. Refresh before repeating an uncertain action.

`tasks delete TASK_REFERENCE --permanent` removes the specified occurrence. Add `--series`
for an active recurring series, then `--confirm-series TASK_REFERENCE` and ordinary
confirmation (`--yes` for non-interactive execution). `tasks complete`, `tasks reopen` and
`tasks delete` support `--dry-run` without application-state creation or credentials.
These plans do not verify the current server item. Permanent deletion leaves no Deleted
Items copy. Actual server regeneration after deletion and full history remain unverified;
deletion checks use synthetic native EWS requests, never real mailbox deletion.

Exchange owns the completion timestamp. Completing a task writes its status and percentage, then reads the server-assigned completion time. Reopening clears completion state without attempting to write the read-only EWS CompleteDate field.
