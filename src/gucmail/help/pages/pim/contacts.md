# Contacts, recipients, and directory search

**Contacts → Contact groups** opens account-local groups that work offline. The
**Manage Contact Groups** palette command also opens this workspace. Choose Mailbox
for personal Exchange groups. Create, edit recipients one per line, or delete an
exact group after review. Local groups survive cache clearing; mailbox deletions
move to Deleted Items. Changed previews are rejected. Linked/nested/unresolved
members remain visible and cannot be silently removed by the editor.

**Compose (Bcc)** reviews fresh explicit recipients and opens a draft; sending has
its own confirmation. `contact-groups --help` documents bounded listing, CRUD and
recipient output. Default CLI groups are local; `--source mailbox` explicitly connects
to the mailbox. Institutional distribution/security groups are outside this editor.

Mailbox contacts are editable Exchange items. Directory results are separate read-only
people records. Explicit recent/frequent reports read bounded Inbox/Sent metadata from
Exchange; composer autocomplete uses separate local successful-send history.

## Find and edit people

Search can consider display name, email, labeled phone and address fields, company, title, categories, and notes when those fields are available. Directory lookup may be disabled and does not imply permission to enumerate an institutional address list.

Next and Previous navigate bounded contact candidate pages. Free text is matched locally against at most 200 candidates per request, so an empty page can still offer Next. CLI list/search support `--offset`; add `--page` for `items`, `scanned`, and `next_offset`. Continue the same query at the returned offset, not the number of matches. A full final window may offer one additional empty page; refresh from zero after concurrent mailbox edits. Sorting and fuzzy ranking apply within each page, and fuzzy search retains explicit email/company/category filters.

**Settings → Contacts** controls the name format, sorting and duplicate threshold for
this account. Names can use the stored display name, given names followed by the
family name, or the family name first. Missing structured names fall back to the
stored name. Applying these controls relabels and reorders an open Contacts page,
retaining its highlighted and selected records without fetching another page.
An open editor keeps the original contact name. JSON, vCard exports and merge
previews also retain the original records. Bare `gucmail contacts`, list and ordinary
search use the saved sorting; explicit fuzzy search keeps its relevance order.

The duplicate threshold is a score from **1 to 100**, initially **70**. Higher values
show fewer suggestions. **Find duplicates** checks only the loaded page and reports
the threshold, score and reasons. `gucmail contacts duplicates --threshold 80`
overrides the saved threshold for that invocation. Detection never merges contacts;
review the separate merge preview and confirmation before changing records.

Use **Tab** to focus the detail pane. Arrow keys, Page Up/Page Down and Home/End
scroll its contact fields or duplicate review; **Shift+Tab** returns to the previous
control. The selected contacts stay selected while you inspect the full explanation.

Create and update plans normalize addresses, validate supported labels, and list every changed field before confirmation. Duplicate detection is local and explainable: exact email or normalized phone matches rank strongly, while similar names are suggestions only. A merge preview must show the primary record, conflicts, combined values, and proposed removals.

**Recipient autocomplete** in Settings → Contacts completes each bare email prefix
in To, Cc and Bcc from at most 20 local recent addresses. Press **Right** at the end
of the field to accept. It preserves previous recipients and typed capitalization,
and skips addresses already in that field. It performs no automatic contact,
directory or mailbox search. Use Compose to contact for an explicit mailbox contact.

**Use and remember recent recipients** stops both recording and use when off,
including the palette's recent-person suggestion. An accepted immediate CLI/TUI
send records To/Cc recipients; **Remember Bcc recipients** is off by default.
Previews, saved drafts, queued/scheduled sends, failures and uncertain results do
not record here. Autocomplete off alone does not stop recording. Changes take
effect when returning to an open composer without changing its text.

History stays in private account-local configuration and is excluded from settings
exports. Turning recording off preserves old entries; clear **Command Palette →
Recently contacted people** with an empty list, then Stage and Apply. Old entries
have no recipient-role metadata and cannot be selectively purged as Bcc. Separate
explicit recent/frequent mailbox reports and full history indexing have their own
scope. Configuration-file backups may contain retained history.

**Exchange directory lookup**, **Contact duplicate suggestions**, **Frequent correspondent
reports** and **Contact mailbox-history access** are separate account controls in Contacts
Settings. Disabled operations stop before connecting or querying. Directory access still
requires server permission. Duplicate discovery off leaves import protection and reviewed
manual merging available. Mailbox-history access off blocks recent/frequent reports,
Correspondence, Open mail, and new index recording. Frequency reports off leaves recent
reports available. Settings Cancel preserves behavior; Apply takes effect on return.

Reports retrieve no bodies or attachments and leave read state alone. Each explicit
Inbox/Sent metadata request reads at most 200 items per folder and returns at most 200
address observations. CLI `--scan` lowers these bounds; counts cover only that window.

**Index observed contact history** is off by default. When enabled, explicit reports retain
only address, timestamp, direction and local message reference in SQLite. Up to 10,000
deduplicated observations are retained for **Contact history retention** days (30 by default,
1–365 allowed). Reads and writes prune expiry. No names, subjects, bodies or attachment
content are retained here. Sent observations can include To/Cc/Bcc; the composer's Bcc
preference governs its separate suggestion history. A failed local index write leaves the
metadata report usable. This index is incomplete and performs no background enumeration.

**Contacts → Local history** inspects up to 100 observations without Exchange. Indexing off
hides retained entries and stops new recording; it does not erase old entries. **Clear local
history** confirms before removing this account's observation index, leaving mailbox items
and composer recent recipients alone. CLI `contacts history-index` provides aggregate
status, `--show` bounded entries when enabled, and confirmed `--clear`. Settings exports
exclude observation data; state-database backups may contain it. Cache clearing removes
this disposable index.

Ambiguous names must be resolved before sending, and reply-all presents normalized
recipients. vCard imports are bounded; exports contain private personal data.
