# Search mail and saved searches

Search can use the live Exchange server, a local index, a completed backup, or bounded local state. Every result should identify its source because coverage and freshness differ.

## Unanswered conversation review

Open **Review Unanswered Conversations** from the command palette, or run `gucmail insights unanswered`. Supply all sending aliases with `--self-address` in addition to the configured account email. Incoming folders default to Inbox and sent folders to Sent Items; both are editable. Analyze reads only a bounded metadata window (100 per folder by default, plus one lookahead record). Enter opens a highlighted message only when requested.

The report matches conversation IDs across those folders. **No reply found** means no later self-authored message was observed in the covered scope. It does not mean a reply is expected. Missing IDs are counted; missing sender/date data, tied timestamps and incomplete later-time coverage are **uncertain**. Raise the per-folder limit or refine the folders when coverage is incomplete. Other folders/accounts and unsaved sent copies are outside this evidence. Changing the TUI scope clears stale results.

## Live search

Supported fields include sender, recipient, subject, text, before/after dates, attachment presence, read state, flag, importance, folder, category, size, and Internet Message ID. Server-supported filters remain on Exchange. A client-side fallback is explicitly labeled and limited; it must not download the whole mailbox.

Examples use synthetic values: `from:teacher@example.invalid unread:true` or `subject:"project update" after:2026-01-01`. Saved searches store the query locally, not a frozen result.

**Search Current Folder** and **Search All Mail** prompt for message text when chosen without a
query. Their explicit scope overrides the default source in Settings. Empty input keeps the prompt
open; Escape cancels without searching. The command name itself is never sent as a query.

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
literal slash in a folder name as `\/`; mailbox selectors are separate from local filesystem paths.

All-mail search includes the mailbox root and ordinary descendant mail folders. It discovers at
most 50 descendant folders and exposes a merged 200-result window. Larger scopes fail with an
instruction to select a folder or use the local index. Calendar/contact/task and search-folder
aliases are excluded. Results are merged by date or the requested size order before pagination.
Bodies and attachment contents are absent from summary pages. Criteria needing extra details fetch
only the globally selected candidates; local matching is limited to at most 200 candidates, so
narrow the server criteria for deeper coverage. Search does not mark messages read.

In the CLI, use `gucmail search --all-folders --subject "synthetic schedule"`; an ordinary text
argument is literal. Advanced and saved expressions use `scope:mailbox AND subject:"synthetic schedule"`.
The scope selector cannot be combined with a folder selector or placed under OR/NOT.

## Choosing a source

Use live search for current server truth. Use the local index for fast or offline text queries after checking its update checkpoint. Use backup search for a point-in-time archive. Fuzzy ranking is useful for typos but should be disabled when automation requires exact matching. See [Local index and fuzzy search](help:search.local) and [Command palette](help:palette.overview).

From the Command Palette, **Search backups**, **Search all data**, **Similar-message search**, **Duplicate-message search**, **Near-duplicate search**, and **Search offline mail** open the local Search workspace. Its table labels every row as `local-index` or `backup · BACKUP_ID`, explains the score, and never connects to Exchange. `Ctrl+F` focuses the query, `Enter` opens a result, `S` compares the highlighted indexed message, `D` shows duplicate evidence, and `N` runs bounded near-duplicate analysis. Encrypted archives are skipped with an unlock instruction rather than prompting for a secret unexpectedly.

**Semantic search** opens optional local corpus-topic ranking. Use **Semantic setup** to review privacy, fields and resource limits, consent, preview/build/update or clear. Results label `local-semantic`; changed indexed text requires update. This uses existing local text only and never calls a remote embedding service. See [Local index and fuzzy search](help:search.local) for the lifecycle and its limits.
