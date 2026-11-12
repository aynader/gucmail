# Configuration and environment variables

Contacts settings include account-local recipient autocomplete, **Use and remember
recent recipients**, and **Remember Bcc recipients** (off by default). Completion
uses bounded local successful-send history; the history opt-out stops its use and
recording. Retained entries can be cleared through **Command Palette → Recently
contacted people**. See [recipient assistance and privacy](../contacts/index.md#recipient-assistance)
for immediate/queued-send boundaries and handling of older Bcc entries.

Contacts also has independent directory, duplicate-suggestion, frequency-report and
mailbox-history controls shared by CLI and TUI. Optional **Index observed contact history**
is off by default, with bounded retention and explicit clearing through **Contacts → Local
history** or `contacts history-index`. See [discovery controls and index privacy](../contacts/index.md#discovery-controls-and-local-history-index)
for exact query, retention, Bcc and backup boundaries.

GUC Mail CLI stores non-secret configuration in a versioned TOML document. Use
`gucmail config path` to print the effective path, `gucmail config show` for a redacted view,
and `gucmail config set NAME VALUE` for supported core fields. The full-screen Settings area
edits the larger settings registry with staged validation and revision history.

The interactive Settings screen uses General, Mail, Appearance, Accounts, and
Advanced tabs. Enter opens a numbered preference row; **Keep change** stages an
edit and **Save** (Ctrl+S) applies it. **Discard** (Ctrl+Z) cancels staged changes.
Ctrl+F searches all settings. The **All … settings** links and Advanced retain
the complete catalog, while More contains history, import/export, and resets.
Older references to Stage/Cancel/Apply describe the same staging and persistence
operations under these updated labels.

Passwords, webhook secrets, tokens, and backup passphrases do not belong in this file. Credential
secrets use the OS keyring or an interactive prompt. Configuration loading rejects secret-looking
plaintext keys rather than silently persisting them.

## Path overrides

These variables are read before the configuration document. Platform defaults come from
`platformdirs` and vary by operating system.

| Variable | Meaning |
|---|---|
| `GUCMAIL_CONFIG_DIR` | Configuration directory |
| `GUCMAIL_DATA_DIR` | Durable state, recovery, index, and backup directory |
| `GUCMAIL_CACHE_DIR` | Disposable cache directory |
| `GUCMAIL_LOG_DIR` | Redacted rotating-log directory |
| `GUCMAIL_CONFIG` | Exact configuration-file path; overrides the file below the config directory |

## Core `[gucmail]` fields

Every field also has an uppercase environment override named `GUCMAIL_<FIELD>`. Environment
values affect the current process but are not written back by an ordinary save. Remove the
override before editing that field in Settings or with `config set`.

| TOML field | Environment variable | Default / constraint |
|---|---|---|
| `email` | `GUCMAIL_EMAIL` | Unset; primary SMTP address |
| `username` | `GUCMAIL_USERNAME` | Unset; Exchange login name |
| `endpoint` | `GUCMAIL_ENDPOINT` | `https://mail.guc.edu.eg/EWS/Exchange.asmx`; HTTPS only, no embedded credentials/query/fragment |
| `default_folder` | `GUCMAIL_DEFAULT_FOLDER` | `inbox`; safe user-facing folder path or role |
| `page_size` | `GUCMAIL_PAGE_SIZE` | `25`; integer 1–200. The TUI may increase this to fill the visible message list, up to 200. Disable **Fill the message list** in settings for a fixed page size. |
| `poll_interval_seconds` | `GUCMAIL_POLL_INTERVAL_SECONDS` | `300`; minimum 60 without the explicit unsafe development override |
| `theme` | `GUCMAIL_THEME` | `builtin.default-dark`; stable built-in/custom theme ID |
| `no_color` | `GUCMAIL_NO_COLOR` | `false` |
| `datetime_format` | `GUCMAIL_DATETIME_FORMAT` | `%Y-%m-%d %H:%M %Z` |
| `timezone` | `GUCMAIL_TIMEZONE` | `Africa/Cairo`; valid IANA zone |
| `external_editor` | `GUCMAIL_EXTERNAL_EDITOR` | Unset; executable/argument string parsed without a shell |
| `download_directory` | `GUCMAIL_DOWNLOAD_DIRECTORY` | Unset; defaults to `Downloads/GUC Mail` |
| `signature` | `GUCMAIL_SIGNATURE` | Empty text |
| `allow_remote_content` | `GUCMAIL_ALLOW_REMOTE_CONTENT` | `false`; retained for compatibility; remote resource rendering is not implemented |
| `cache_enabled` | `GUCMAIL_CACHE_ENABLED` | `true` |
| `cache_message_bodies` | `GUCMAIL_CACHE_MESSAGE_BODIES` | `false`; automatic body caching is not implemented |
| `index_attachments` | `GUCMAIL_INDEX_ATTACHMENTS` | `false`; optional extractors are still capability-gated |
| `semantic_search_enabled` | `GUCMAIL_SEMANTIC_SEARCH_ENABLED` | `false`; no remote embedding fallback |
| `monitoring_enabled` | `GUCMAIL_MONITORING_ENABLED` | `false` |
| `command_hooks_enabled` | `GUCMAIL_COMMAND_HOOKS_ENABLED` | `false`; compatibility field; each `watch --command-hook` invocation requires confirmation |
| `request_timeout_seconds` | `GUCMAIL_REQUEST_TIMEOUT_SECONDS` | `30`; positive integer |
| `retry_attempts` | `GUCMAIL_RETRY_ATTEMPTS` | `3`; integer 0–10 |
| `retry_base_seconds` | `GUCMAIL_RETRY_BASE_SECONDS` | `2.0`; positive number |
| `retry_max_seconds` | `GUCMAIL_RETRY_MAX_SECONDS` | `120.0`; at least the base delay |
| `max_attachment_bytes` | `GUCMAIL_MAX_ATTACHMENT_BYTES` | `52428800` (50 MiB); positive integer |
| `autosave_seconds` | `GUCMAIL_AUTOSAVE_SECONDS` | `30`; minimum 5 |
| `undo_send_seconds` | `GUCMAIL_UNDO_SEND_SECONDS` | `0`; non-negative |
| `logging_level` | `GUCMAIL_LOGGING_LEVEL` | `WARNING`; `CRITICAL`, `ERROR`, `WARNING`, `INFO`, or `DEBUG` |
| `redact_email_addresses` | `GUCMAIL_REDACT_EMAIL_ADDRESSES` | `true` |
| `mode` | `GUCMAIL_MODE` | `basic`; `basic`, `advanced`, or `expert` |
| `onboarding_complete` | `GUCMAIL_ONBOARDING_COMPLETE` | `false` |
| `expert_raw_soap_enabled` | `GUCMAIL_EXPERT_RAW_SOAP_ENABLED` | `false`; does not expose arbitrary XML execution |

Boolean environment values accept `1/true/yes/on` and `0/false/no/off`, case-insensitively.
Numeric conversion and the same configuration validation run before application startup.

## Attachment download choices

In **Settings → Mail → Attachment collision behavior** (`mail.attachment_collision`),
choose `rename` (the default), `error`, or `overwrite`. The choice uses account scope and
supports Stage/Cancel/Apply, Reset, Undo, import/export and restart. Applying a new default
updates an open attachment workspace that has no per-download override. The download
folder remains a separate machine setting; it is omitted from portable exports.

Both `gucmail attachments save` and `save-all` use the saved choice. Their `--collision`
option overrides it for one invocation. The workspace collision control is also a
per-download override. Choosing overwrite does not authorize replacing existing files:
review their exact paths and confirm in the TUI, or inspect `--dry-run` before using
the global `--yes` option in automation.

Batch saves prepare every bounded target before downloading content. Rename reserves
distinct names for attachments whose normalized names collide. Error and overwrite reject
duplicate targets within a batch; save those individually with distinct names or choose
rename. A file that appears after preview is preserved and reported as a collision.
Download failures may leave earlier completed files; completed files are not rolled back.
Attachments are saved without opening or executing them.

## Message ordering

In **Settings → Mail**, choose **Message sort field** (`mail.sort_field`) and
**Message sort direction** (`mail.sort_direction`). Fields are received date, sent date,
subject, sender, size and importance. The default remains received date descending.
These preferences use account scope and participate in Stage/Cancel/Apply, Reset, Undo,
import/export, environment overrides and restart.

Applying them in a connected TUI refreshes the current bounded folder page using server
ordering. Existing selection is preserved when the item remains in that page. Staging or
canceling does not fetch a page or write configuration. Explicit search ranking is independent
of folder ordering. A server that rejects an ordering returns an error instead of quietly
sorting only the already loaded page.

`gucmail messages list` uses the saved order; `--order-by=-size` or another supported explicit
order overrides it for that invocation. `--limit` is bounded at 200 and `--offset` remains a
server page offset. An invalid order is rejected before credentials are resolved. For example:

```text
gucmail config set mail.sort_field subject
gucmail config set mail.sort_direction ascending
gucmail --json messages list --limit 25
gucmail --json messages list --order-by=-size --limit 10
```

## Conversation grouping and ordinary Delete

These account preferences use Settings Stage/Cancel/Apply, Reset, Undo, import/export and restart.

| Setting ID | Default and behavior |
|---|---|
| `mail.group_conversations` | `false`; TUI grouping by exact Exchange conversation ID within the currently loaded page. Each message keeps its own row and action target. The `[1/2]` subject prefix counts only visible page members, not the entire conversation. Missing IDs and similar subjects do not form groups. |
| `mail.delete_behavior` | `deleted_items`; ordinary Delete moves to Deleted Items. Choose `move_to_folder` to use the next setting instead. Confirmation is always required. |
| `mail.delete_folder` | Unset; an existing folder selector, path or ID. It is required when `move_to_folder` is active; the confirmed move checks availability. No folder is created automatically. |

Grouping changes only the TUI presentation of bounded folder pages and search results. It fetches
no extra messages, hides no rows, preserves selected references and restores the original page
order when turned off. CLI list/JSON output retains its server order. Applying grouping does not
change read state or implicitly select an entire conversation for a bulk action.

Ordinary Delete uses the saved destination in the TUI and `messages delete`. A CLI `--to FOLDER`
overrides that destination for one invocation; `--behavior deleted_items` selects Deleted Items
regardless of a saved custom folder. Review with `--dry-run` before using global `--yes` for
automation. Plans and dry runs do not resolve credentials or connect to Exchange. The TUI review
captures the exact message and destination; changing selection or preferences while it is open
does not retarget the confirmed move.

Permanent deletion remains an explicit separate action with its existing confirmation. It cannot
become a saved default, and CLI `--permanent` cannot be combined with `--to` or `--behavior`.
An uncertain provider result is not proof that a move failed; refresh before attempting it again.

## Reply, forward and Sent Items defaults

In **Settings → Mail**, these account preferences affect new compositions. Stage and
Cancel leave saved choices unchanged; Apply persists them. Reset, Undo, settings transfer
and restart use the same canonical values. A saved or recovered draft retains its own body,
recipients and Sent-copy choice when these defaults change.

| Setting ID | Default and behavior |
|---|---|
| `mail.default_reply_mode` | `sender`; ordinary Reply can instead start with all visible recipients. Explicit Reply all always uses all. Your account address and duplicate addresses are excluded from reply-all; Bcc is never inherited. |
| `mail.reply_include_history` | `true`; sender replies start with bounded, inert quoted text. |
| `mail.reply_all_include_history` | `true`; independent original-text choice for reply all. |
| `mail.forward_include_history` | `true`; include original text in a forward. This does not remove inherited server attachments, which remain disclosed in the send review. |
| `mail.save_sent_copy` | `true`; request an Exchange Sent Items copy. Turning it off adds a review warning. |

`reply --reply-mode sender` overrides the recipient default. Reply, reply-all and forward
accept `--include-history` or `--no-history`. New send/response/draft-create commands accept
`--save-sent-copy` or `--no-sent-copy`; `drafts edit` can change a saved draft's choice.
The TUI composer has a **Sent copy: on/off** checkbox and includes it in the exact send review.
Local drafts, recovery and scheduled/Undo jobs preserve the reviewed choice. Changing it
after scheduling requires an explicit job review, rather than silently changing a due send.

The Sent-copy preference is local send policy, not a field stored in an unsent EWS item.
A new import of a server-only draft starts with the safe `true` choice; server edits and
conflict resolution preserve an existing local draft's choice. Legacy local drafts/recovery
and queued jobs retain their historical `true` behavior. An uncertain send is never replayed;
without a Sent copy, establish delivery manually before intentionally sending another message.
Exchange acceptance does not by itself prove recipient delivery or a fresh Sent-folder readback.

## Composer preferences

Use **Settings → Composer & Drafts** or `gucmail config set ID VALUE`. These preferences seed
new messages, replies and forwards. Existing saved/recovered drafts keep their reviewed choices.

| Setting ID | Default and behavior |
|---|---|
| `composer.default_body_format` | `plain`; `html` edits HTML source as text and preserves it for delivery |
| `composer.default_importance` | `normal`; also accepts `high` and `low` |
| `composer.request_read_receipt` | `false`; requests a receipt without guaranteeing a response |
| `composer.request_delivery_receipt` | `false`; requests a receipt without guaranteeing a response |
| `composer.signature_enabled` | `false`; insert `composer.signature` into new compositions |
| `composer.signature` | Empty; private text, excluded from exports and redacted status |
| `composer.warn_missing_attachment` | `true`; warning about attachment wording outside quoted history |
| `composer.recipient_warning_threshold` | `20`; integer 0–500, zero disables this warning |
| `composer.empty_subject_policy` | `require`; `warn` permits an empty subject after send confirmation |

Non-core IDs also accept environment overrides such as
`GUCMAIL_SETTING_COMPOSER__DEFAULT_BODY_FORMAT=html`. `composer.signature` uses the core
`GUCMAIL_SIGNATURE` override. Validation and attachment safety checks remain active when warnings
are disabled. [Compose Help](../user-guide/index.md) describes the ordinary review workflow.

## Header and status preferences

Use **Settings → Layout** to stage and apply these choices. The same IDs work through
`gucmail config set`, settings import/export, and environment overrides.

| Setting ID | Default and behavior |
|---|---|
| `layout.header_contents` | `account,storage,monitoring,folder`; choose and reorder these fields, or use an empty list |
| `layout.header_compactness` | `adaptive`; also `compact` or `detailed`, always one row high |
| `layout.status_bar_contents` | `activity`; append optional `connection`, `folder`, or `monitoring` |

Removing `storage` or `monitoring` hides the corresponding header indicator. `activity`
stays first in the status bar so operation results remain visible. These choices do not
cause mailbox requests. For example, set `GUCMAIL_SETTING_LAYOUT__HEADER_CONTENTS=account,folder`.

## Named layout presets

Use **Tools → Saved layouts** or `gucmail layouts` to save, list, load and remove named
arrangements. Presets include the twelve display preferences for pane visibility/proportions,
split orientation, reader mode, message columns and header/status fields. They do not include
account credentials, message contents, signatures or theme choices.

`layout.presets` is the global, bounded library (at most 32 names, each 1–40 printable characters,
unique ignoring letter case). Each entry must contain exactly the supported layout fields;
normal Settings validation rejects malformed imports before writing. Prefer the guided screen
or CLI to editing this structured setting. Loading atomically applies the display choices and
records `layout.last_preset` as `saved:NAME`. A removed entry leaves the active layout intact.

```text
gucmail layouts save Reading
gucmail layouts load Reading --dry-run
gucmail layouts load Reading
gucmail layouts save Reading --replace
gucmail --yes layouts delete Reading
```

The CLI saves the configured arrangement; the TUI saves its currently displayed arrangement,
including session adjustments. Library changes participate in Settings import/export, reset,
history and Undo. Revision conflicts require a refresh; saving never silently replaces newer
configuration. Layout commands do not connect to Exchange.

## Document shape and recovery

The current document schema is version 5:

```toml
schema_version = 5

[gucmail]
email = "student@example.test"
page_size = 25
theme = "builtin.default-dark"

[settings]
"layout.preview_visible" = true
```

The `[settings]` table contains IDs from the searchable settings registry; use `gucmail settings
list`, `settings search`, or the TUI Settings area rather than guessing IDs. Saves are atomic and
revision-checked. The application keeps a last-known-good copy and bounded revision history,
migrates older schemas, preserves unknown future keys, and quarantines malformed primary files.
The revision identifies the exact bytes loaded or committed. Recovery and migration use the
same conflict check as ordinary saves; if another process changes the file, reload before
applying changes. On Windows, transient sharing failures receive at most five short retries
(0.77 seconds of total delay). Persistent denial preserves the existing file and reports failure.

## Automation examples

PowerShell:

```powershell
$env:GUCMAIL_PAGE_SIZE = "50"
$env:GUCMAIL_NO_COLOR = "true"
gucmail --json messages list --limit 10
```

POSIX shells:

```sh
GUCMAIL_PAGE_SIZE=50 GUCMAIL_NO_COLOR=true gucmail --json messages list --limit 10
```

Do not place `GUCMAIL_PASSWORD` on shared command lines or in checked-in shell profiles. For
credential precedence and removal, see [Privacy and security](../privacy-and-security/index.md).


## Migration and operational settings

### Keyboard assignments

Help → Keys and the palette's Keyboard Shortcuts command browse the current screen
and control bindings. Filters include Mail, Composer, Calendar, Contacts, Tasks,
Backup, Search and Command palette. Choose **Edit keys**, or Customize on an
application action, to open the guided assignment editor. **Settings → Keyboard →
Action assignments → Edit keys** opens the same editor. The two palette keys and
previous/next theme keys also retain their exact Settings rows. The browser
identifies fixed controls and context-specific reuse.

An empty assignment disables that key. Ctrl+K and Ctrl+P remain palette defaults.
Use Ctrl/Alt plus a supported key or F1-F24. Select an action, enter a key and choose
**Stage** (Ctrl+Enter); **Apply** (Ctrl+S) validates every action and all four global
keys together. Stage both sides of a swap before Apply. A failed conflict check
preserves the saved file and staged choices. **Cancel** (Ctrl+Z) discards staged
changes. An action's original default can be restored even when it is a bare key.
Text editing, clipboard, navigation, confirmation and system control keys stay
protected; separate screens may reuse a key for different actions.

Applied keys update the current screen and future screens. Root mailbox remaps stay
within the mailbox workspace. **Reset key** stages one default; **Reset all** asks
for confirmation and stages unlocked defaults, with Apply still separate.
Environment and policy overrides remain locked. Saved keyboard changes participate
in Settings history and Undo.

Choose a file path and **Export** to save the configured action map and four global
keys only. An existing file needs replacement confirmation. **Preview** reads a
complete keyboard export (maximum 4 MiB) into the Proposed column; **Import** asks
for confirmation before applying it. Preview and Cancel leave saved keys untouched.
Imports reject missing or extra setting fields, unknown actions, invalid keys and
conflicts. Concurrent configuration edits invalidate the review and require a new
preview or Refresh. Export requires staged changes to be applied or canceled first.

Invalid saved/environment combinations activate safe defaults with a repair notice
and preserve the saved file. For an invalid saved action map, open **Help → Keys →
Edit keys**, confirm Reset all, then Apply to repair it. Remove an invalid environment
override before editing. The map stores only known action IDs and keys; it accepts
neither arbitrary commands nor executable action definitions.

Version 5 archives the former generic quota fallback as migration metadata. It preserves configured
Exchange warning/send/receive thresholds; the 92 MiB GUC Prohibit Send fallback is not total capacity.
Nullable explicit settings use the TOML marker `{__gucmail_null__ = true}` so saving an explicit
None value does not silently restore an inherited value. Prefer the Settings editor to hand edits.

Calendar display policy is account scoped. `calendar.default_view` chooses `agenda`, `day`,
`work_week`, `week` or `month` when opening Calendar or running bare `gucmail calendar`.
Explicit CLI subcommands and the current TUI selector override the default for that view only;
changing the default applies to newly opened workspaces. `calendar.week_start` is Monday,
Saturday (default) or Sunday. Week views and complete month grids share that alignment; month
views include adjacent-month dates and mark them outside the primary month in JSON. Each view
performs one bounded query (at most 200 events); the limit can underfill its dates.

`calendar.timezone` uses an IANA zone (default `Africa/Cairo`) and the existing `GUCMAIL_TIMEZONE`
environment override. Default dates and Today use that zone rather than the host's local date.
Timed events display as local instants with explicit offsets, including both sides of a DST fold.
All-day dates retain their event zone and an exclusive end date. JSON, export, and existing-item
edit forms preserve original timestamps and event-zone metadata. Settings Apply updates a mounted
Calendar's presentation locally; its next refresh captures the new zone and week start for query
boundaries. Open availability forms retain their captured zone, offsets and working policy.
View/week-start environment overrides are `GUCMAIL_SETTING_CALENDAR__DEFAULT_VIEW` and
`GUCMAIL_SETTING_CALENDAR__WEEK_START`. Invalid zones fail before provider resolution.

Calendar working-time preferences are account scoped. `calendar.working_days` accepts a
nonempty, unique comma-separated list of full weekday names (or a JSON string list), defaulting
to Monday–Friday. `calendar.workday_start` and `calendar.workday_end` default to `09:00` and
`17:00` in the configured display time zone. These are editable user defaults, not a GUC timetable.
The end must follow start on the same day; stage both endpoints before Apply when needed.
Invalid, overnight or equal windows fail before saving. Cancel, import, reset and Undo use the
normal canonical Settings transaction. Environment variables are
`GUCMAIL_SETTING_CALENDAR__WORKING_DAYS`, `GUCMAIL_SETTING_CALENDAR__WORKDAY_START` and
`GUCMAIL_SETTING_CALENDAR__WORKDAY_END`.

Work-week views select configured weekdays within the configured seven-day week and retain
all events overlapping those local days, including all-day and overnight events. They perform
one bounded calendar query, so a result limit can underfill the filtered view. Full week and
month views retain every day and event hour. Free-time suggestions require the entire meeting
inside one working-day window; day boundaries follow local DST, while meeting duration measures
elapsed minutes. Ambiguous or nonexistent working-hour boundaries fail explicitly. CLI
`calendar free-time --all-hours` or clearing **Use working days/hours** in the TUI overrides
this policy for one explicit window. Searches are bounded to 31 elapsed days and 100 attendees;
missing or incomplete availability never confirms a slot. Open availability forms keep their
captured policy; a new form or Calendar refresh reads newly applied preferences.

Calendar and Task creation defaults are account scoped and apply when opening a fresh form or
building a CLI create preview. `calendar.default_duration_minutes` defaults to 60 (1–10,080);
`calendar.default_reminder_minutes` defaults to none (0–525,600 whole minutes before start);
`calendar.default_availability` defaults to `busy`. A supplied end overrides the duration.
Timed durations measure elapsed minutes across daylight-saving changes. All-day events without
an end use the next local midnight; the timed duration setting does not apply.

`tasks.default_due_days` defaults to none (0–3,650 whole days after the specified start date,
or the creation date in the configured time zone). `tasks.default_reminder_minutes` defaults
to none (0–525,600 whole minutes after creation, independent of due date).
`tasks.default_importance` defaults to `normal`. Blank optional numbers disable these defaults;
Settings labels disabled reminders **Reminder off** and an absent due date **No due date**.
Zero enables a reminder at start/creation or a due date on the base date. Explicit form edits
and command options take precedence. Apply changes affect the next fresh form, including forms
prefilled from a message; open forms, edits of existing items and imported records keep their
values. These settings initialize Exchange item properties, not a local notification schedule.

`tasks.default_repeat` defaults to `none`; fixed-date choices are `daily`, `weekly`, `monthly`
and `yearly`. `after_completion_daily`, `after_completion_weekly`, `after_completion_monthly`
and `after_completion_yearly` ask Exchange to create the next task the selected number of
days, weeks, months or years after completing the current one.
`tasks.default_repeat_interval` defaults to 1; daily accepts 1–999, weekly/monthly 1–99,
and yearly requires 1. All four after-completion choices accept 1–999. Change interdependent
values together in a staged Settings session.
`tasks.default_repeat_count` defaults to none; 1–999 includes the first occurrence and blank
means **No end**. These account-scoped values seed only fresh forms and CLI creation plans.
The supplied start date, otherwise due date, otherwise configured local creation date anchors
the rule. `--repeat none` disables repetition; `--repeat-count`, `--repeat-forever` and
`--repeat-until` override its boundary for one command. See [Tasks](../tasks/index.md) for scope
and examples. Exchange owns regeneration and completion dates; these defaults do not create
a local scheduler or full historical occurrence archive. Fixed-date RRULE conversion cannot
represent after-completion rules; use their typed JSON representation.

Date-format and time-zone preferences apply to the TUI mail list and reader. Structured timestamps
and CLI table timestamps retain ISO formatting.

Use **Settings → Start at login → Open setup** to manage actual OS startup. The legacy boolean is
not the task's installed/enabled status. Provider secrets have dedicated secure setup controls.
Remote resource rendering, automatic message-body caching, automatic disk-cache quota enforcement,
and generic experimental flags are not implemented; their Settings entries explain this and are
read-only. Explicit backups and indexing provide offline content. Legacy file fields do not enable
these absent implementations. Command hooks require per-invocation authorization.

A state directory is bound to one mailbox and endpoint. An existing unbound directory is adopted
once. Use a separate `GUCMAIL_DATA_DIR` for a different mailbox; switching accounts does not erase
or repurpose the previous account's local references, drafts, or history.


### Contact display and duplicate preferences

Account Settings includes `contacts.sort_order` (`display_name`, `family_name`,
`company`), `contacts.display_name_format` (`stored`, `given_family`, `family_given`)
and `contacts.duplicate_threshold` (integer 1–100, default 70). Name formatting changes
presentation only; structured output, editing, vCard exports and merge identity retain
stored fields. Missing structured names fall back to the stored name. Sorting covers
one bounded candidate page, with continuation offsets preserved. Explicit fuzzy search
retains its relevance order.

Stage and Cancel leave the current Contacts page unchanged. Apply re-renders a mounted
page locally, retaining highlighted and selected references without a new query or
restarting its worker. New and refreshed workspaces read the effective account values.
An already open contact editor keeps its original form fields. Duplicate detection
uses the saved threshold and never merges automatically; higher values yield fewer
suggestions. CLI `contacts duplicates --threshold 80` overrides the threshold for one
invocation, including an environment default, without saving it. Invalid thresholds
are rejected before accessing contacts.

Automation can use `GUCMAIL_SETTING_CONTACTS__SORT_ORDER`,
`GUCMAIL_SETTING_CONTACTS__DISPLAY_NAME_FORMAT` and
`GUCMAIL_SETTING_CONTACTS__DUPLICATE_THRESHOLD`. These override effective values without
writing the configuration file. Recent recipients, frequent correspondents, history
indexing, directory preference and privacy policies have separate behavior and scope.

### Task views and sorting

Account `tasks.default_view` defaults to `open`, with `due`, `all`, `today`, `this_week`,
`overdue` and `completed` also available. CLI task lists and new TUI workspaces use it;
an existing workspace retains its selected view. `tasks.show_completed` defaults false
and applies to All, Today and This week. Open, Due and Overdue always exclude completed
tasks; Completed explicitly selects them. Date windows use `calendar.timezone` and do
not reinterpret or rewrite the task's stored date fields.

`tasks.sort_order` defaults to `due_date`, with `subject`, `importance` (high first)
and `status` choices. Sorting affects each bounded candidate page, preserving its
continuation offset; it does not guarantee folder-wide ordering. Due-date sorting
puts incomplete tasks first and undated tasks after dated tasks in each group.
Stage/Cancel/Apply, account isolation, transfer, reset and Undo use the normal Settings
transaction. Apply updates loaded candidates locally without fetching more data.
Refresh to obtain a window using the new visibility or date restrictions; a changed
view, visibility or date restarts continuation at zero. Editors keep their captured
values. Recurring-task defaults and completion/history semantics have separate scope.

Environment overrides are `GUCMAIL_SETTING_TASKS__DEFAULT_VIEW`,
`GUCMAIL_SETTING_TASKS__SHOW_COMPLETED`, `GUCMAIL_SETTING_TASKS__SORT_ORDER` and
`GUCMAIL_TIMEZONE`. CLI `tasks list --view ... --sort ... --completed/--no-completed`
overrides preferences for one invocation without saving them. Explicit search status
takes precedence over completed visibility; a saved default view never narrows a search.

### Attachment index policy

Current-machine `search.index_attachments` is off by default. It controls deliberate CLI
build/update/rebuild and Expert Diagnostics rebuild after a separate confirmation. Stage/Cancel
do not start downloads; Apply persists the policy for subsequent operations. Changing settings
after review invalidates it. Disabling stops future extraction and retains existing private index
data until confirmed clear. See [attachment indexing](../search/index.md#deliberate-attachment-content-indexing).

| Setting | Default | Allowed range / behavior |
|---|---|---|
| `search.attachment_formats` | `csv,html,json,txt,xml` | Core text; explicitly selected PDF/DOCX/PPTX/XLSX use optional installed dependencies; PNG/JPG require OCR consent |
| `search.attachment_max_bytes` | 5 MiB | 1–10 MiB per file |
| `search.attachment_timeout` | 8 seconds | 1–15 seconds per parser/recognizer; Exchange request timeout is separate |
| `search.attachment_files_per_message` | 5 | 1–20 |
| `search.attachment_files_per_run` | 25 | 1–100 content downloads |
| `search.attachment_total_bytes` | 20 MiB | 1–100 MiB per run, including probe bytes |
| `search.attachment_output_chars` | 50,000 | 1–100,000 retained per message |
| `search.attachment_ocr_enabled` | `false` | Separate local image recognition consent |
| `search.attachment_ocr_language` | `en-US` | One installed language; no engine/language installation |

Generic overrides use `GUCMAIL_SETTING_SEARCH__ATTACHMENT_FORMATS`,
`GUCMAIL_SETTING_SEARCH__ATTACHMENT_MAX_BYTES`, `GUCMAIL_SETTING_SEARCH__ATTACHMENT_TIMEOUT`,
`GUCMAIL_SETTING_SEARCH__ATTACHMENT_FILES_PER_MESSAGE`, `GUCMAIL_SETTING_SEARCH__ATTACHMENT_FILES_PER_RUN`,
`GUCMAIL_SETTING_SEARCH__ATTACHMENT_TOTAL_BYTES`, `GUCMAIL_SETTING_SEARCH__ATTACHMENT_OUTPUT_CHARS`,
`GUCMAIL_SETTING_SEARCH__ATTACHMENT_OCR_ENABLED` and `GUCMAIL_SETTING_SEARCH__ATTACHMENT_OCR_LANGUAGE`.
`GUCMAIL_INDEX_ATTACHMENTS` overrides the core enable flag. Environment overrides do not write
preferences and block editing that value. Bounds count input/output, not total process memory.
