# Contacts and people

## Quick explanation

Contacts are editable items in the user's mailbox. Directory results are separate, read-only people
records supplied by Exchange when the account has permission. Local recent/frequent correspondents
are inferred from the user's own bounded history and are not uploaded elsewhere.

## Find and inspect contacts

```text
gucmail contacts list
gucmail contacts search "Example Person"
gucmail contacts show CONTACT_REFERENCE
gucmail directory search "Example Person"
gucmail directory resolve "specific.address@example.edu"
gucmail contacts correspondence CONTACT_REFERENCE
gucmail recipients recent
gucmail recipients frequent
```

Use **Contacts → Contact groups** for account-local or personal mailbox groups. The
[contact-group guide](groups.md) covers explicit membership, conflict detection,
reviewed Bcc composition and the boundary around linked/nested/directory groups.

Search may use display name, names, email addresses, phone labels, company, department, title,
categories, and notes. Directory lookup is capability-gated and may be unavailable; the client does
not assume access to the GUC global address list.

Specific email, company, and category filters run on Exchange before the result limit.
Free-text search spans additional formatted fields and notes, so it filters a bounded
candidate window (at most 200 contacts) locally after those specific restrictions.
Use Next/Previous in the Contacts screen to continue through later candidate windows,
including an empty window that still offers Next. CLI `contacts list` and `contacts search`
accept `--offset`; add `--page` to include `items`, `scanned`, `offset`, and `next_offset`
in the result. Repeat the same query with the returned `next_offset` until it is null.
The default result remains an array, with continuation instructions in warnings.
Offsets count examined server candidates, not matching contacts. A full final window may
offer one additional empty page. These are changing mailbox views, not snapshots; refresh
from zero after concurrent edits. Sorting and fuzzy ranking apply within each page.
Fuzzy search uses contact names, email addresses, company, and categories, retaining the
explicit email/company/category restrictions. Specific-filter searches retrieve metadata
without notes; free-text searches request the notes needed for that search.

## Create and edit

```text
gucmail contacts create --display-name "Example Person" --email person@example.edu
gucmail contacts edit CONTACT_REFERENCE --phone mobile=0123456789
```

The create/edit plan validates the display name, normalized email addresses, labeled phones and
postal addresses, dates, and categories. Review every changed field before confirmation. Exchange
uses fixed labels for several contact fields, so an unsupported label may need mapping.

## Duplicates and merge

```text
gucmail contacts duplicates
gucmail contacts merge CONTACT_REFERENCE...
```

Duplicate detection is local and explainable. Email and normalized phone matches rank strongly;
approximate names can suggest candidates. Merge first produces a preview containing the primary
record, combined values, conflicts, and records proposed for removal. Nothing changes until that
plan is confirmed. Preserve the originals or export them before a large merge.

## Import and export

```text
gucmail contacts import example.vcf
gucmail contacts export CONTACT_REFERENCE --output example.vcf
```

vCard input is size- and record-bounded. Import generates a plan and never modifies the Exchange
directory. Exported vCards contain private addresses, phones, and notes. Existing export targets are
never replaced unless `--overwrite` is supplied and the overwrite is confirmed.

## Recipient assistance

Mailbox contacts can be composed to directly from the Contacts screen. Directory results remain
read-only and are shown with explicit ambiguity status; `directory resolve` succeeds only for one
result. Recent/frequent summaries and per-contact correspondence use bounded, body-free mailbox
metadata and do not transmit local history elsewhere.

**Settings → Contacts → Recipient autocomplete** completes bare email prefixes in
each To, Cc, and Bcc entry, including later entries in a comma-separated list.
Press **Right** at the end of the field to accept the visible suggestion. Typed
addresses, names and capitalization are preserved; an address already present in
that field is not suggested again. Completion uses at most 20 account-local recent
addresses. It does not automatically load contacts, search a directory, scan mail,
or transmit typed text. Compose to a mailbox contact through Contacts explicitly.

**Use and remember recent recipients** controls both recording and using this
history, including the palette's recently contacted person. Recording follows an
accepted immediate send through the CLI or TUI. Previews, draft saves, queueing,
scheduled delivery, failures and uncertain sends do not add history here. Disabling
autocomplete alone leaves recording enabled. **Remember Bcc recipients** is off
by default; enable it only if Bcc addresses should appear in local suggestions.

Turning history off preserves existing entries. Clear them in **Settings → Command
Palette → Recently contacted people** by staging an empty list and applying it.
Older entries have no To/Cc/Bcc role metadata, so turning Bcc recording off cannot
identify or remove old Bcc entries. History remains private local configuration
and is excluded from settings exports. Ordinary backup of your configuration
file may still contain it.
These controls do not govern the separately requested bounded mailbox
recent/frequent-correspondent reports or establish a complete contact-history index.

## Discovery controls and local history index

Settings → Contacts also provides **Frequent correspondent reports**, **Exchange
directory lookup**, **Contact duplicate suggestions**, and **Contact mailbox-history
access**. These apply to both CLI and TUI operations. Turning directory lookup off
blocks explicit search and resolution before connecting. Turning duplicate suggestions
off disables discovery; import duplicate protection and reviewed manual merges remain
available. Directory lookup still depends on the server's ResolveNames permission.

Mailbox-history access governs explicit recent/frequent reports, contact correspondence,
and Open mail. Turning it off blocks those queries and new history indexing. The separate
frequent-report switch leaves recent reports available. Reports use metadata only, with
at most 200 items from each Inbox/Sent folder and 200 resulting observations. CLI `--scan`
can reduce both limits, and `--since` is sent to Exchange and checked locally. Counts
describe the stated observation window rather than the entire mailbox. No bodies or
attachments are fetched, and read state is unchanged.

**Index observed contact history** is off by default. When enabled, explicitly requested
metadata reports retain email address, timestamp, direction, and local message reference
in the account-bound SQLite database. Names, subjects, bodies and attachment content are
excluded. Sent observations may include To, Cc and Bcc addresses. The composer Bcc-history
preference governs only composer recent recipients, so disable history indexing if these
mailbox observations should not be retained.

The index holds at most 10,000 deduplicated observations; **Contact history retention**
defaults to 30 days from the message timestamp and accepts 1–365 days. Reads and writes
prune expired entries. There is no automatic mailbox enumeration or background indexer.
Incomplete observations never replace live reports as the mailbox source of truth. A local
index failure leaves a successful metadata report usable and emits a generic warning.

**Contacts → Local history** shows up to 100 retained observations without connecting to
Exchange. Turning indexing off stops recording and hides existing observations; it does
not erase them. **Clear local history** has a confirmation and removes this account's
index even when indexing is off. It leaves the separate composer recent-recipient list
and mailbox items alone. `gucmail contacts history-index` shows aggregate status;
`--show --address person@example.test --limit 20` reads the local index when enabled;
`--clear` requires confirmation (`--yes` for automation). Cache clearing also removes this
disposable index. Observation data is excluded from ordinary settings exports but remains
private personal data in backups of the state database.

## Troubleshooting

- Directory search denied: continue with mailbox contacts and local history.
- Contact changes conflict: refresh the contact; its Exchange change key may be stale.
- Merge suggestion is wrong: exclude it. Suggestions are not automatic mutations.
- Contact photo unavailable: photo behavior depends on attachment and server support.

## Security and privacy

The local contact index is optional and removable. Contact exports and inferred correspondence
history stay local. Command output and logs redact addresses according to configuration.

## Read a saved backup offline

In offline mode, open this area and select an explicit completed local backup. The screen shows
its source date and warns that the saved copy can be stale. Search covers this area's saved
records across the selected backup before paginating matches. It runs in a cancellable worker;
changing the query returns to the first result page. Encrypted backups require the passphrase.
This view does not contact Exchange or offer server mutations. Attachment contents are not
read for this search; it covers saved fields and attachment metadata.
