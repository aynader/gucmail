# Search, indexing, fuzzy matching, and clusters

## Quick explanation

Search has several explicit sources. Server search asks Exchange to filter bounded folders. The local
metadata/full-text index adds fast offline queries. Fuzzy search tolerates spelling mistakes.
Optional semantic search ranks locally similar text. Clusters group mailbox items for exploration.
Every result identifies its source: `live_server`, `cache`, `search_index`, `backup`, or `local`.

## Basic and field search

```text
gucmail search "project update"
gucmail search --sender teacher@example.invalid --after 2026-01-01 --has-attachments
gucmail search --all-folders --subject "synthetic schedule"
```

Search fields include sender, recipient, subject, body, before/after dates, attachments, read state,
flag, importance, folder, category, size, and Internet Message ID. Server-supported filters stay on
Exchange. A filter that needs local post-processing is labeled as a fallback and remains bounded.
The visual TUI query builder provides the same fields without requiring syntax.

Common syntax:

```text
from:teacher@example.invalid
to:student@example.invalid
subject:"exact phrase"
after:2026-01-01 before:2026-09-01
has:attachment attachment:pdf
unread:true flagged:true importance:high
folder:inbox category:"Example category"
```

Use explicit ISO dates in scripts. Without `--folder` or `--all-folders`, the CLI searches the
configured default folder. **Search Current Folder** and **Search All Mail** in the TUI palette
prompt for message text; the command name is never used as the query. Their explicit scope takes
precedence over the configured default search source. Message text entered in that prompt is literal.

During live mail search, the status bar shows loading and Escape cancels the pending view.
A newer mail search or folder refresh supersedes the previous request. Cancelled, timed-out, or
superseded reads cannot replace the current results, scope, or status when their responses arrive.
The in-flight Exchange request may still finish; this is cancellation of its local result, not a
promise to stop the server operation. Search-history metadata committed before cancellation can
remain, while the session preference never stores query text or automatically runs a search.

**Open Unread Mail** and **Open Flagged Mail** search the configured default mail folder.
They remember that concrete folder scope and preserve it when a later **Search Mail** prompt is
reopened; they do not replay the unread/flagged query automatically. A saved current search keeps
its explicit folder selector, even if the configured default folder changes later.

Relative role paths such as `inbox/Courses` start below the distinguished Inbox even when its
display name is localized. A discovered absolute path selects the display hierarchy and can
disambiguate a custom folder named like a role (for example `/Mailbox/Inbox/Courses`). Escape a
literal slash in a folder name as `\/` and a literal backslash as `\\`; mailbox selectors are separate
from local filesystem paths. Direct lookup and child listing preserve the full parent path. Local
folder moves and renames update retained paths under the known source path. If another client
reuses a saved path for a different folder, refresh the folder tree and select the folder again;
the saved reference cannot silently switch to the replacement.
When its old path is missing, a saved folder reference can instead recover current metadata by
identity through bounded folder-only reads. Recovery stops on mismatched identity or malformed
ancestry and does not fetch message bodies or attachments.

`--all-folders` searches the mailbox root and ordinary mail folders throughout its hierarchy;
calendar/contact/task and search-folder aliases are excluded. Discovery is capped at 50 descendant
folders, and the merged result window at 200 items (`offset + limit`). Each folder supplies only
its bounded, server-filtered prefix; results are merged before applying the global offset. A larger
hierarchy or result window fails with a narrowing instruction instead of returning a partial mailbox.
Mail can change between requests; this is a read-only search, not a transactional mailbox snapshot.

Summary retrieval excludes bodies and attachment contents. When a criterion needs body, recipient,
header or attachment-name details, only the globally selected candidates are fetched (at most 200).
Residual matching can therefore miss matches beyond that candidate window; narrow the server-side
criteria or use the local index. No message is marked read. This path is verified with synthetic
folders and the installed library's query/payload builders; it has not been probed on the live account.

Expert expressions in the TUI Advanced Search and saved searches can use `scope:mailbox`, for example
`scope:mailbox AND subject:"synthetic schedule"`. This selector must be a positive top-level condition
and cannot be combined with a folder selector or placed under `OR`/`NOT`. Saving the current TUI
mailbox search preserves that selector. The CLI's plain text argument remains literal; use its
field flags or a saved expression when scripting structured criteria.

## Saved searches

```text
gucmail saved-searches add example-unread 'unread:true after:2026-08-01'
gucmail saved-searches list
gucmail saved-searches run example-unread
gucmail saved-searches remove example-unread
```

Saved searches store the query locally, not its result. Running one later uses the selected live or
local source. Names are scoped to the active account, so two configured accounts may use the same
name without overwriting each other. Advanced expressions are stored as parsed query data and are
executed without flattening `OR` or `NOT` into a different plain-text search. Each run records a
bounded history entry plus its time and result count when **Search history** is enabled in Settings.
Disabling history stops new history rows while still updating the selected saved search's own
last-run time, result count, and run count.

In the TUI, open **Search · Saved searches** from the command palette. The browser shows the name,
scope, query preview, exact server/local execution explanation, last-run time, result count, and run
count. It supports create, run, edit, rename, duplicate, and confirmed delete. **Recent** switches to
bounded search history; the filter box searches names, queries, scopes, and execution details.
Press `Enter` to run, `Ctrl+N` to create, `E` to edit, `R` to rename, `D` to duplicate, `Delete` to
remove, `H` for recent history, or `F1` for this Help page. Use `Tab` and `Shift+Tab` to reach the
scrollable action row in narrow terminals. An empty account gets a guided create-first state rather
than a blank table.

Creating from **Search · Save current search** pre-fills the editor with the current advanced
expression. Changes and run metadata are committed transactionally to the local state database and
survive application restart. Saved-search migrations preserve compatible existing rows; they do not
delete or silently reset the database.

## Local full-text index

```text
gucmail index status
gucmail index build
gucmail index update
gucmail index rebuild
gucmail index verify
gucmail index optimize
gucmail index clear
```

The local index is optional derived data, not a backup. The mailbox builders index one
bounded page of mail summaries: subject, sender, folder, date, size and any supplied preview.
`--include-bodies` deliberately fetches message details for safe body text, recipients and
attachment names. Index construction does not mark mail read. Header/category metadata may
be retained in a detail payload; it is not a separate FTS search field. Calendar, contact and
task search in saved backups uses the separate archive-search workflow.

Build/update commits each row independently. If a later read fails, earlier row progress
remains; repeat the bounded page to refresh it. No dedicated build checkpoint is claimed.
Rebuild replaces the entire disposable index with the selected page, after confirmation.
It prepares the new documents before one SQLite transaction, rejects duplicate references,
more than the reviewed limit (maximum 500), or more than 32 MiB of prepared UTF-8 field/payload
data. This bound does not include source buffers or total process memory. A read/validation
failure, SQL error, cancellation observed before commit, or a changed index revision retains
the previous usable index, including its FTS data and update marker. An empty replacement
explicitly clears it. The CLI can fetch selected bodies; Diagnostics never fetches bodies.
Both use the saved attachment policy described below. Connections close explicitly after commit/rollback, including read operations.

In the TUI, choose **Expert** in Settings → Advanced → Settings complexity, then open
**Rebuild index** from the palette. It opens the exact folder/limit preview after the screen
mounts. Declining leaves the index alone. The reviewed write identity also detects an edit
that has the same row count and clock timestamp; a conflict asks you to review again.

`cache_message_bodies`, `index_attachments`, and `semantic_search_enabled` are false by default.
The shared mailbox producer reads the effective current-machine **Attachment text indexing**
policy for deliberate CLI build/update/rebuild and Diagnostics rebuild. Disabled means no
attachment metadata or content read. Enabled builds require a separate review/confirmation
before any content download (`--yes` for reviewed automation); changing policy after preview
invalidates it. Saving a preference alone starts no background job.

### Deliberate attachment content indexing

Under Settings → Search & Indexing, enable **Attachment text indexing**, choose the allowed
file types and limits, Stage and Apply. Cancel discards staged changes. Current-machine values
survive restart; environment overrides affect execution and block editing that value.
TXT, CSV, JSON, XML and HTML are allowed by default, including `.htm`. PDF, DOCX, PPTX and XLSX
require explicit format selection and their already installed optional parser dependencies.
Missing dependencies and unsafe, encrypted, failed, disabled or oversized files receive
labelled outcomes; no dependency is installed by the index builder.

Defaults: 5 MiB input per file (maximum 10 MiB), 8 seconds per parser/recognizer (1–15),
5 files per message (maximum 20), 25 downloads per run (maximum 100), 20 MiB downloaded bytes
per run (maximum 100 MiB, including probe bytes), and 50,000 retained extracted characters
per message (maximum 100,000). Known oversize files are skipped before opening; unknown sizes
use bounded stream reads and close the stream. Limits also apply when metadata is inaccurate.
Exchange downloads use the connection request timeout separately. No Windows process-memory
quota is claimed. Parser cancellation stops the owned child; cancellation observed before
the rebuild commit retains the old index. Build/update may retain earlier per-row progress.

Image indexing requires both PNG/JPEG in **Attachment index file types** and the separate
**Image OCR in attachment indexes** opt-in. **Attachment index OCR language** chooses one
installed language (default `en-US`). The local Windows recognizer uses the same bounded OCR
service as the per-image workflow, including image dimension/pixel limits. No engine or language
is downloaded. Temporary owned images are removed afterward; this is not forensic erasure.
OCR output is labelled `ocr_attachment_text` and potentially inaccurate. A missing engine,
language or invalid image leaves a labelled outcome and does not block ordinary file extraction.

Extracted text, sanitized filenames and per-file outcomes become private `index.sqlite3` data,
with per-message/per-file truncation recorded. Only aggregate counts are printed by a build.
Local attachment search uses the resulting filename/text fields without downloading again.
Disabling a preference stops future extraction; use confirmed `index clear` to remove existing
derived content. Rebuild replaces the entire index by the selected bounded page, so choose its
scope deliberately. Physical backups/filesystem snapshots may retain earlier data.

## Fuzzy search

```text
gucmail search "synthetci project" --fuzzy
gucmail search "synthetci project" --fuzzy-threshold 82
gucmail contacts search "Exampel Person" --fuzzy
```

The default threshold is `82`. Exact matches rank above approximate matches. Approximate results
show why they matched. Candidate sets are bounded, and punctuation/spacing are normalized without
changing displayed source text. Disable fuzzy behavior when exact automation matters.

## Optional semantic search

`local-lsa-v1` is an optional, corpus-trained TF-IDF/SVD topic model. It learns word
co-occurrence from the selected local text and ranks normalized topic projections with
lexical term overlap (default weights 0.8/0.2). A related term can match when the query word
is absent, provided those relationships occur in the training corpus. It has no pretrained
language knowledge or guaranteed multilingual understanding. See the primary
[SVD documentation](https://numpy.org/doc/stable/reference/generated/numpy.linalg.svd.html)
and [LSA method description](https://scikit-learn.org/1.8/modules/generated/sklearn.decomposition.TruncatedSVD.html).
The implementation uses NumPy for fitting; scikit-learn is not a dependency.

It is disabled by default. Installing the optional `semantic` extra (`numpy==2.3.5`) is
a deliberate environment change: `python -m pip install 'gucmail[semantic]'`.
The application never installs that dependency, downloads a pretrained model or sends
indexed text to a remote AI service. Queries of an existing validated model use standard
Python arithmetic and remain usable without NumPy. A missing fitting dependency produces
a safe diagnostic while lexical search remains available.

```text
gucmail semantic status
gucmail --yes semantic enable
gucmail semantic build --limit 500 --dimensions 32 --subjects-only
gucmail --yes semantic build --limit 500 --dimensions 32 --subjects-only --apply
gucmail semantic search "deadline change" --limit 50 --semantic-weight 0.8
gucmail semantic update
gucmail --yes semantic update --apply
gucmail semantic build --include-body --rebuild
gucmail --yes semantic build --include-body --rebuild --apply
gucmail semantic disable
gucmail --yes semantic clear
```

Build/update first previews the scope and privacy/resource limits. `--apply` plus confirmation
performs the local write. It uses **existing** indexed subjects, and existing indexed body
chunks only after `--include-body`; it never fetches mailbox bodies, addresses, attachments
or headers. The separate `index build` command has its own mailbox-read scope. Initial
defaults are 500 latest local records, subjects only and at most 32 topic dimensions.
Subsequent updates reuse the saved scope and fitted basis. `--rebuild` retrains; changing
body policy or requested dimensions requires this explicit choice. New or changed records
are transformed incrementally and records absent from the complete bounded selection are
removed. Growth can make the selected latest window differ; coverage remains labelled.

The selection is limited to 2-2000 records. Subjects are limited to 8192 characters;
optional bodies to 32768 per record in four chunks. The first 2048 chunks train at most
2048 vocabulary terms and 96 dimensions. Dense fitting input is at most 32 MiB; SVD working
memory is additional. Fitting runs in an owned child process with a 60-second deadline,
network access denied, and cooperative cancellation terminating only that child. Model JSON
is bounded to 8 MiB and the SQLite store to 64 MiB; these are analytical limits, not an OS
memory quota. Model fitting can fail or time out on an expensive selection; the prior
model remains usable. Cancellation is checked during transformation and before commit.

The account identity binds the model to the configured address and endpoint. A hash binds
searches to the exact selected indexed fields and scope. Changed/deleted source records
cause a stale diagnostic until update, including after restart. A changed preview or source
during fitting is rejected, and revision checks prevent competing builds or clear operations
from silently overwriting each other. Transactions roll back failed writes. Status/query
read existing databases without creating or migrating them; an unknown schema is preserved.

`semantic.sqlite3` in the application data directory contains the model vocabulary and basis,
title/folder metadata, lexical term overlap data and embeddings. Raw body chunks are not
stored there, but derived data still reveals content and must be protected. Disabling
stops semantic processing and keeps the model until `clear`. Confirmed clear removes the
derived records/model and compacts SQLite; it retains the lexical index, mail and backups.
This does not guarantee erasure from filesystem snapshots or physical forensic recovery.

In the TUI, open **Semantic search** from the command palette and use **Semantic setup**.
The setup form offers consent, bounded scope, optional body chunks, preview, confirmed
build/update/retrain, disable and confirmed clear. `Tab`/`Shift+Tab` reach scrollable controls;
`Ctrl+C` cancels an active operation. Search rows identify `local-semantic`, show the hybrid
score and explain the topic/lexical components. The fitted selection limits coverage;
the lexical/fuzzy/backup modes remain separate available sources.

## Local Search workspace

The TUI Command Palette routes **Search backups**, **Search all data**, **Search offline mail**,
**Similar-message search**, **Duplicate-message search**, and **Near-duplicate search** to a real
local workspace. It federates the disposable SQLite index and completed unencrypted archives,
including archived PIM records, with explicit `local-index` and `backup · BACKUP_ID` labels.
Encrypted archives are skipped with an unlock instruction; no passphrase is requested or retained
by a search command.

Similarity scans at most 5,000 local records. Exact duplicate status requires a complete-content or
raw-MIME SHA-256 digest; shallow matches remain **possible** and cannot seed cleanup. Near-duplicate
comparison is bounded, all result explanations are visible, and Search performs no mutation. The
optional Semantic command opens local topic search and its setup controls, with a
privacy/model notice when disabled or unbuilt and no remote embedding service.

## Cluster exploration

```text
gucmail clusters build --by topic
gucmail clusters list
gucmail clusters show CLUSTER_REFERENCE
gucmail clusters export CLUSTER_REFERENCE
```

Local strategies include `topic`, `conversation`, `sender`, `sender_domain`, `recipient_group`,
`time_period`, and `attachment_type`. A cluster shows its explanation, count, unread count, range,
top people/terms, attachment types, representative messages, folder distribution, and estimated
storage when data exists. Clustering defaults to bounded, deterministic local algorithms; it does
not train or call a cloud service.

A cluster can seed a dynamic search, backup, notification rule, or cleanup plan. Each follow-up is a
separate previewed action. Building a cluster never deletes or sends mail.

## Unanswered conversation review

```text
gucmail insights unanswered --self-address me@example.test --self-address alias@example.test
gucmail --json insights unanswered --folder inbox --folder "Projects" --sent-folder sent --limit 200
```

The configured account email is included automatically. Add every address or alias you send from.
In the command palette, open **Review Unanswered Conversations**, review the addresses and incoming/
sent folders, then Analyze. Enter opens a selected message through the usual reader. Changing scope
clears the old results and cancels an in-flight scan. Opening the form does not connect to Exchange.

The shared service reads metadata in pages of at most 200, with a default of 100 records per folder
and one lookahead record. The limit can be raised to 1,000 per folder, across at most ten explicitly
selected folders. No message body or attachment content is read, and no read flags change.

Conversations use Exchange conversation IDs, never subject similarity. A later self-authored message
removes a conversation from the candidate list. **No reply found** means no later self message was
observed in the covered folders; newsletters and FYI messages may not need a reply. Other folders,
accounts, unsaved sent copies, missing aliases and messages arriving during the scan are outside this
evidence. Missing IDs are counted, and incomplete time coverage, missing sender/time fields and tied
incoming/outgoing timestamps produce **uncertain** results. JSON includes each folder's scanned and
fetched counts, completion state and timestamp boundary. Increase the limit or refine the selected
folders to investigate uncertain results.

## Attachment-content search

Core safe extraction formats are TXT, CSV, JSON, XML, and HTML. PDF, DOCX, PPTX, and XLSX require
the `attachments` optional dependency and explicit enablement. Default extraction limits include a
10 MiB input, 1,000,000 output characters, an 8-second timeout, and isolated processing.

Ordinary text extraction never runs macros or scripts, follows links, or performs OCR.
Optional image OCR has the separate explicit local workflow described below. It rejects DTD/entity XML,
external OOXML relationships, encrypted documents, unsafe archives, oversized data, and unavailable
dependencies with a recorded status such as `unsafe`, `too_large`, `timeout`, or `encrypted`.

## Search explanations and troubleshooting

When results surprise you, inspect source, parsed query, resolved dates, scope, fallback fields,
ranking explanation, fuzzy score, and index checkpoint. Update an index before rebuilding it. A
backup may be intentionally stale; offline mode displays its backup date.

## Security and privacy

Indexes and embeddings can contain private message, contact, calendar, task, and attachment text.
Keep the data directory private and erase derived stores when no longer needed. `index clear` and
`semantic clear` are local destructive actions and require clear scope; they do not change Exchange.

## Optional local image OCR

Use `ocr status` to inspect the installed Windows recognizer languages. The application
does not install or download a language pack, model, engine, or dependency. Recognition
requires Windows and a usable installed OCR runtime; ordinary text extraction, FTS and
semantic search remain independent when OCR is unavailable.

```text
gucmail --json ocr status
gucmail --json ocr extract saved-image.png --language en-US
gucmail --yes --json ocr extract saved-image.png --language en-US --apply
```

The default command previews one already saved local image. `--apply` explicitly opts
in, with confirmation (`--yes` for reviewed automation). In the normal palette, open
**Open Local Image OCR**, select a local file and an installed language, Preview, enable
OCR for that image, then confirm Recognize. Tab/Shift+Tab reach the scrollable controls
and result; Ctrl+C cancels, and Escape returns. Changing file/language/limits clears the
preview, consent and result. A declined confirmation does not recognize anything.

The separate OCR service supports PNG and common baseline/progressive JPEG headers.
It checks input bytes, header CRC/dimensions where available, and pixel bounds before
the native decoder. Limits: 8 MiB input, 2048 pixels per dimension, 4,194,304 pixels,
first image only, up to 100,000 output characters (default 50,000), and a recognition
worker deadline of 1-15 seconds (default 8). Language inspection has its own 8-second
bound. No OS memory quota is claimed. Network paths, mapped network drives and symbolic
links/reparse points are rejected. An owned hidden process receives JSON data, uses
the installed local language and rechecks actual decoded dimensions. No shell
interpolation, profile, script-policy bypass or image text execution is used.

The image and language availability are rechecked after confirmation. An owned
temporary image copy in the OS temporary directory is removed after processing;
timeout/cancellation stops only the owned worker. Temporary-file deletion is not a
forensic/snapshot-erasure guarantee. Raw native errors and inherited application
secrets are excluded from the worker protocol. Malformed/oversized input, missing
languages, invalid results, worker failures, cancellation and timeout have safe outcomes.

Recognized text is terminal-sanitized and labelled `ocr_attachment_text`; truncation
is exposed. OCR can be inaccurate and this engine supplies no confidence score.
Review text before relying on it. This operation fetches no mailbox attachment,
performs no cloud recognition, changes no mail, and writes no index. Its consent applies
to the selected operation; the next form starts disabled. The separate current-machine
attachment index policy above controls deliberately requested index builds and never
inherits consent from this image form.
