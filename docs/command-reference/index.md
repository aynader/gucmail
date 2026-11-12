# Command reference

This page inventories the installed command names. Offline help is authoritative for arguments and
options:

```text
gucmail --help
gucmail GROUP --help
gucmail GROUP COMMAND --help
```

Global options include `--config`, `--json`, `--ndjson`, `--no-color`, `--quiet`, `--verbose`,
`--debug`, `--non-interactive`, and `--yes`. Put global options before the command.

Calendar and Task create commands use account-scoped creation defaults. Calendar `--end` is
optional; omitting it uses the saved duration. `--duration-minutes` overrides that duration and
conflicts with `--end` or `--all-day`. Use `--reminder-minutes`/`--no-reminder` and `--availability`
for explicit event values. Tasks supports `--no-due` and `--no-reminder` alongside the existing
`--due`, `--reminder-at` and `--importance` options. Each positive/disable pair is mutually
exclusive. Their `--dry-run` previews validate locally without resolving credentials;
non-interactive creation still requires `--yes`. Existing item edits and imports keep their fields.

`messages delete` uses the account's ordinary Delete destination: Deleted Items by default,
or the folder chosen in Settings → Mail. `--to FOLDER` overrides the destination; `--behavior
deleted_items` explicitly chooses Deleted Items. Every Delete requires confirmation. Use
`--dry-run` for a local plan without credentials or an Exchange connection, then global `--yes`
for a deliberately approved non-interactive invocation. `--permanent` is irreversible, ignores
the ordinary destination preference, and cannot be combined with either destination override.

```text
gucmail --json messages delete MESSAGE_REF --dry-run
gucmail --json messages delete MESSAGE_REF --to inbox/Archive --dry-run
gucmail --json --non-interactive --yes messages delete MESSAGE_REF --behavior deleted_items
```

## Root commands

`version`, `login`, `logout`, `status`, `doctor`, `diagnostics`, `tui`, `send`, `reply`,
`reply-all`, `forward`, `search`, `health`, and `watch`.

## Mail and local state

| Group | Commands |
|---|---|
| `folders` | `list`, `tree`, `create`, `rename`, `move`, `delete`, `empty` |
| `messages` | `list`, `show`, `headers`, `raw`, `search`, `move`, `copy`, `delete`, `restore`, `mark-read`, `mark-unread`, `flag`, `unflag`, `categorize`, `export`, `operations` |
| `drafts` | `send-status`, `list`, `show`, `create`, `edit`, `send`, `delete`, `duplicate`, `schedule`, `recover`, `conflicts`, `sync`, `save-server`, `resolve-conflict` |
| `attachments` | `list`, `save`, `save-all` |
| `saved-searches` | `list`, `add`, `remove`, `run` |
| `config` | `show`, `set`, `path` |
| `settings` | `list`, `categories`, `get`, `search`, `set`, `reset`, `export`, `import`, `history`, `undo` |
| `layouts` | `list`, `save`, `load`, `delete` |
| `help` | `show`, `list`, `categories`, `toc`, `search`, `commands`, `shortcuts`, `glossary`, `error`, `validate` |
| `cache` | `status`, `clear` |

`attachments save` and `save-all` use the saved `mail.attachment_collision` choice;
`--collision rename|error|overwrite` overrides it for that invocation. `save-all`
prepares a bounded batch (`--limit`, default 100, maximum 200) and checks all overwrite
confirmations before downloading any file. `--dry-run` inspects metadata and planned paths
without downloading content. Existing targets require global `--yes` in non-interactive
automation. `--sha256` calculates each completed download's digest. Duplicate batch
destinations require rename or individual saves with distinct names. Concurrent file
creation can still cause a safe collision failure; earlier completed downloads remain.

## People, calendar, and tasks

| Group | Commands |
|---|---|
| `calendar` | `list`, `agenda`, `day`, `week`, `month`, `search`, `show`, `history`, `create`, `edit`, `update`, `delete`, `cancel`, `respond`, `availability`, `free-time`, `conflicts`, `import`, `export`, `watch` |
| `contacts` | `list`, `search`, `show`, `create`, `edit`, `update`, `delete`, `merge`, `duplicates`, `groups`, `import`, `export`, `correspondence`, `history-index` |
| `contact-groups` | `list`, `show`, `create`, `edit`, `delete`, `recipients` |
| `directory` | `search`, `resolve` |
| `recipients` | `recent`, `frequent` |
| `tasks` | `list`, `search`, `show`, `history`, `create`, `edit`, `update`, `complete`, `reopen`, `delete`, `export` |

`tasks create --repeat` accepts fixed-date daily/weekly/monthly/yearly rules and the four
`after_completion_*` choices. The latter preserve Exchange task regeneration in typed JSON;
RRULE conversion rejects it explicitly. When completing a recurring task returns a new item,
the returned local reference identifies the completed instance and the original reference
still identifies the next task. A failure to read the accepted result exits with code 10;
inspect or refresh current state before repeating a write. See [Tasks](../tasks/index.md).

`tasks complete`, `tasks reopen` and `tasks delete --dry-run` plan locally without credentials.
Ordinary task deletion moves the stored task and its recurrence to Deleted Items. Permanent
occurrence deletion uses `--permanent`; whole-series deletion also requires `--series` and
the exact reference in `--confirm-series`, followed by ordinary confirmation. A local preview
does not verify recurrence on the server. The TUI provides these choices through **Delete options**.

`calendar watch` reports bounded native Exchange event changes with a resumable quiet baseline;
`--forever` continues at the configured interval. Contact `groups` are category groupings, not
distribution-list mutation. `contact-groups` manages account-local groups by default;
explicit mailbox selection enables bounded personal SMTP-group workflows. Linked/nested or
unresolved members remain read-only. `recipients` returns reviewed SMTP addresses for scripting
and never composes or sends mail. The TUI Groups workspace can prepare a reviewed Bcc draft.

## Scheduling, backup, search, and analysis

Mail response defaults live in Settings → Mail. `reply --reply-mode sender|all` overrides
ordinary Reply's saved recipient mode; explicit `reply-all` always uses all visible recipients.
Reply/reply-all/forward accept `--include-history` / `--no-history`. Forwarded server
attachments remain inherited and disclosed even when quoted text is disabled.
`send`, all response commands and `drafts create` accept `--save-sent-copy` / `--no-sent-copy`;
`drafts edit` accepts the same choice. It appears in send review and is preserved through local
drafts, recovery and scheduling. `--dry-run` never sends or creates a due job. Saved drafts keep
their reviewed choice when defaults change; changing a scheduled draft requires renewed review.


| Group | Commands |
|---|---|
| `scheduled` | `list`, `show`, `edit`, `history`, `reschedule`, `cancel`, `undo`, `send-now`, `run-once`, `run`, `install`, `uninstall` |
| `scheduler` | `status`, `run`, `run-once`, `pause`, `resume`, `doctor`, `install`, `uninstall` |
| `headless` | `validate`, `status`, `health`, `stop`, `pause`, `resume`, `run` |
| `startup` | `plan`, `status`, `enable`, `install`, `repair`, `disable`, `uninstall`, `remove` |
| `recurring` | `list`, `show`, `create`, `pause`, `resume`, `skip`, `delete`, `materialize` |
| `backup` | `list`, `create`, `incremental`, `resume`, `estimate`, `inspect`, `verify`, `browse`, `search`, `export`, `restore-plan`, `restore`, `restore-status`, `prune` |
| `index` | `status`, `build`, `update`, `rebuild`, `verify`, `optimize`, `search`, `clear` |
| `ocr` | `status`, `extract` |
| `semantic` | `status`, `enable`, `disable`, `build`, `update`, `search`, `clear` |
| `clusters` | `build`, `list`, `show`, `search`, `rebuild`, `export` |
| `insights` | `overview`, `analyze`, `storage`, `senders`, `unanswered`, `duplicates`, `quota` |
| `cleanup` | `analyze`, `plan`, `execute` |

## Integration and EWS groups

| Group | Commands |
|---|---|
| `quota` | `show`, `folders`, `local` |
| `ews` | `operations`, `capabilities`, `describe`, `support-matrix`, `diagnostics`, `request-preview`, `delegates` |
| `notifications` | `status`, `test`, `history`, `rules`, `profiles`, `providers`, `desktop` |
| `webhooks` | `list`, `add`, `remove`, `enable`, `disable`, `test`, `migrate-secrets` |
| `automatic-replies` | `show`, `set` |
| `inbox-rules` | `list`, `show`, `create`, `enable`, `disable`, `delete` |

`automatic-replies set` reviews the exact policy before writing and verifies readback.
`inbox-rules create` supports simple rules created disabled; enable/disable and exact-ID
deletion preserve Outlook rule data. Both policy groups support local dry-run plans and
explicit confirmation; server/account permissions remain capability-specific. After an
uncertain write, refresh the policy before attempting another change.

An exposed command name is not a claim that the current server or account allows it. Capability-
gated commands return the unsupported or unauthorized error without pretending success.

`notifications desktop status` inspects the owned Windows notification registration.
`notifications desktop setup` and `notifications desktop remove` show a preview by default;
use `--apply` to review and confirm a change, or `--apply --yes` for intentional automation.
See [Windows desktop notifications](../integrations/windows-desktop.md) for prerequisites,
account binding, removal, and the remaining native verification limits.

`scheduled list` and `backup list` return at most 200 records by default; use their bounded
`--limit` options when another page size is needed. `notifications history` returns 50 append-only
local outcomes by default. NDJSON expands both list and tuple result sets into one record per item
followed by an accurate summary count.


### Calendar views

`calendar history EVENT_REFERENCE --limit 100` explicitly reads a series' current first,
last, modified and deleted occurrence metadata. Selecting an occurrence or exception resolves
its real master while preserving the original local reference. The limit applies separately
to modified/deleted lists (1–200 each); missing fields remain unavailable and an extra entry
marks truncation. Human timestamps use the display zone; JSON retains original times.
This read-only view is not a complete audit trail or expanded occurrence schedule.
See the [Calendar guide](../calendar/index.md) for server-response and restore limits.

Bare `gucmail calendar` uses the account's saved default view, week start and display zone.
`agenda`, `day`, `week` (`--work-week`) and `month` explicitly choose one view without saving
that choice. Omitted dates use Today in the configured zone. Month includes adjacent-month
dates in complete weeks aligned to the saved first weekday; JSON marks primary-month dates.
Views use one bounded query. Human timestamps use the display zone; JSON events retain their
original timestamps and event-zone metadata. All-day dates retain the original event zone.

### Calendar working time

`calendar respond EVENT_REFERENCE` uses the account's saved initial response; supplying
`accept`, `tentative` or `decline` explicitly overrides it. Use `--dry-run` to review the
exact plan, then confirm a deliberately approved mutation. `--save-only` requests a Calendar
update without sending a response where supported. No preference responds automatically.

`gucmail calendar week 2026-10-06 --work-week` uses saved working weekdays and week start.
A full `calendar week` and `calendar month` retain nonworking days and all event hours.

```text
gucmail calendar free-time --attendee guest@example.test --start 2026-10-04T08:00:00+03:00 --end 2026-10-06T18:00:00+03:00 --duration-minutes 45
```

This read-only command makes one bounded attendee free/busy request and returns the first
confirmed shared slot within saved local working days/hours. Add `--all-hours` to search the
entire explicit window. The request requires UTC offsets, lasts at most 31 elapsed days and
contains at most 100 unique attendees. Every covered grid cell must be free for every requested
attendee; failed, missing and incomplete responses return an unconfirmed suggestion. JSON
reports `start`, `end`, `complete` and `reason`; a complete response with no fitting slot has
null start/end. No meeting is created. `calendar availability` remains the raw free/busy view.


### Contact preferences

Bare `gucmail contacts`, `contacts list` and ordinary `contacts search` use the saved
account sort order within one bounded page. `--page` retains `offset`, `scanned`,
`next_offset` and residual information; continue from `next_offset` rather than the
number of matches. Explicit `--fuzzy` preserves relevance order. Human contact names
use the saved name format, while JSON/NDJSON and vCard exports retain original fields.

`gucmail contacts duplicates` uses the saved duplicate threshold, initially 70 on a
1–100 scale. Add `--threshold 80` for an ephemeral override. It examines at most 200
contacts, reports explainable candidate scores and reasons, and never merges contacts.
Use `contacts merge` to inspect and confirm a separate mutation plan.

`contacts history-index` reports aggregate account-local observation counts without connecting
to Exchange. `--show` displays a bounded page only when optional contact-history indexing is
enabled; `--address ADDRESS` narrows that page. `--clear` requires confirmation and removes
this account's observations without changing the indexing preference. Indexing is off by
default and records only explicit bounded correspondence reports. See the [Contacts guide](../contacts/index.md)
for retention, privacy and the distinction from composer recipient history.

### Task preferences

`gucmail tasks list` uses the saved account view, completed visibility and sorting.
Override them locally with `--view all|open|due|today|this_week|overdue|completed`,
`--sort due_date|subject|importance|status` and `--completed` / `--no-completed`.
Open, Due and Overdue always exclude completed tasks; Completed selects them explicitly.
Due includes today and earlier dates, Today selects the configured local date, This week
selects today through six days later, and Overdue selects dates strictly before today.
These dates use the configured time zone. Undated tasks remain available in All and Open.

`tasks search` uses explicit text/status/date criteria, without the saved default view.
It shares ordinary completed visibility and sorting; explicit `--status completed`
still returns completed tasks. `--overdue` selects incomplete tasks strictly before the
configured local date. Contradictory completed/overdue criteria fail locally.
One candidate page is fetched and sorted; JSON/NDJSON records retain original task
metadata. List/search accept `--offset` and `--page` with `scanned` and `next_offset`;
continue from the returned candidate offset, even after an empty result page. Limits
remain at most200 candidates per request. No read command changes task state.

`ocr status` inspects installed Windows OCR languages. `ocr extract IMAGE --language TAG`
previews an already saved PNG/JPEG; `--apply` and confirmation recognize it locally.
`--timeout` (1-15 seconds) and `--max-chars` (1-100000) bound the owned worker and output.
No language download, mailbox fetch or automatic indexing occurs.

### Message operation recovery

`messages operations list --limit 50 --offset 0` lists account-scoped local mutation journals.
`messages operations show OPERATION_ID` shows exact completed, failed, uncertain and unstarted targets.
Both work without credentials or an Exchange connection and never replay a mutation.
Partial effects return exit code 10 with additive structured outcomes in JSON. A dispatched write
without a persisted acknowledgment remains uncertain after restart; review before retrying.
