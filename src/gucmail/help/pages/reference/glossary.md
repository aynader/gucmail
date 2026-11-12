# Glossary

Definitions start with the user-facing meaning. Technical detail follows only where it helps explain behavior or safety.

## Mail and people

**Archive** — A mailbox folder or exported collection used to keep mail outside the everyday Inbox. An Exchange Archive folder is not automatically a verified GUC Mail backup.

**Attachment** — A file or another item carried by a message. GUC Mail lists metadata first and downloads content only when asked.

**Bcc** — Blind carbon copy. Bcc recipients receive the message, but other recipients do not see their addresses in the normal recipient list.

**Category** — A named label stored on a message, contact, calendar event, or task when Exchange supports it.

**Cc** — Carbon copy. A visible recipient who receives the message but is usually not its primary addressee.

**Contact** — A person or organization record in the mailbox. It is different from a read-only directory result or locally remembered recent recipient.

**Conversation** — Exchange metadata grouping related messages. It can help navigation but does not prove every message has identical recipients or subject.

**Deleted Items** — The normal Exchange folder for recoverably deleted items. Permanent deletion and retention expiry may bypass or remove this recovery path.

**Draft** — Editable unsent content. It can be a local recovery copy, durable local draft, Exchange draft, synchronized draft, or conflict.

**Flag** — Follow-up state attached to a message, often with a due date or completion state.

**Folder** — A mailbox container such as Inbox, Sent Items, or a nested custom folder. Display names may be localized; stable paths and IDs avoid assuming English names.

**Importance** — Sender-selected low, normal, or high priority. It is not a security classification.

**Internet Message ID** — A mail transport identifier normally carried in the `Message-ID` header. It differs from the Exchange item ID and local reference.

**Item reference** — A short stable local handle for a long Exchange ID. GUC Mail updates its mapping when Exchange changes an item's identifier after a move.

**Outbox** — The durable local queue and history for delayed, retrying, uncertain, and completed sends. It is separate from Exchange's server folder of a similar name.

**Reply all** — A reply addressed to the sender and other appropriate original recipients after duplicate and self-address handling. Always review the resulting list.

**Scheduled message** — A reviewed draft queued for future transmission. It may use Exchange native deferral when verified or the local scheduler fallback.

**Task** — A mailbox to-do item with fields such as due date, reminder, status, percent complete, and recurrence when supported.

## Local data and offline use

**Backup** — An independently verifiable, self-describing private archive intended for durability, recovery planning, or offline access.

**Cache** — Disposable local data that reduces repeated work. Clearing it can slow the next load but must not delete server mail or a backup.

**Checkpoint** — Durable progress recorded after a successful bounded operation. Watchers, indexes, schedules, and backups use checkpoints to resume without starting over.

**Cluster** — A deterministic grouping of locally indexed items by explainable similarity. It is an exploration aid, not an automatic deletion decision.

**Index** or **search index** — Searchable data derived from mailbox or backup content. It can be rebuilt and is not a backup.

**Offline mode** — Operation without contacting Exchange. Verified backup browsing, eligible local-index search, drafts, schedules, settings, and Help can remain available.

**Restore** — Creating supported items from a backup after a preview. Old Exchange IDs, some extended fields, and some recurrence details cannot always be recreated.

**SQLite** — The local embedded database format used for non-secret durable state such as references, checkpoints, history, and schedules.

**Synchronization** — Comparing durable progress with Exchange changes to update local state without re-reading the whole mailbox.

## Interface and commands

**CLI** — Command-line interface. It accepts scriptable subcommands, options, structured output, and meaningful exit codes.

**Command Palette** — The universal in-process command finder. It searches commands and Help, interprets local natural language, previews parameters and safety, and requests clarification.

**Context** — Facts available to a command, such as the current folder, selected message, open draft, or active screen. Missing context should trigger clarification, not a guess.

**Fuzzy search** — Matching that tolerates spelling mistakes or close wording. Exact field filters remain preferable for scripts and destructive scope.

**Natural-language command** — An ordinary phrase interpreted locally and deterministically into a typed command preview. No external AI service receives the text.

**NDJSON** — Newline-delimited JSON. Each line is a complete JSON record, making event streams and bounded command results easy to pipe.

**TUI** — Terminal user interface. It is the full-screen keyboard-driven application with folders, message list, reader, status, and product-area screens.

## Exchange and identifiers

**Change Key** — Exchange's version token for an item or folder. A mutation using a stale key can be rejected so a newer version is not overwritten.

**Exchange** — Microsoft's mail, calendar, contact, and task server product. GUC Mail connects to the configured server rather than Microsoft Graph.

**EWS** — Exchange Web Services, the HTTPS/SOAP interface used by this client for supported mailbox operations.

**Extended property** — An Exchange field outside the common item model. Expert views can display useful safe properties, but unsupported fields are not fabricated.

**Polling** — Checking periodically for changes. Notification delay is normally up to one polling interval; polling is not described as instant.

**Quota** — Server thresholds or configured evidence describing mailbox storage limits. Warning, Prohibit Send, and Prohibit Send & Receive are distinct values.

**SOAP** — The XML message protocol used by EWS. Raw SOAP operations are isolated behind typed adapters and expert diagnostics.

**Streaming notifications** — Long-lived EWS notification events that can reduce detection delay when the server and account support them. Durable synchronization remains authoritative after disconnects.

## Monitoring and integrations

**Digest** — A grouped notification delivered on a schedule instead of one alert per event.

**Headless** — Running the watcher or scheduler without opening the TUI, normally under Windows Task Scheduler, systemd, or another supervisor.

**HMAC** — A keyed signature that lets a webhook receiver verify that a payload came from a holder of the shared secret and was not modified in transit.

**Idempotency** — The property that retrying the same event does not create unintended duplicates. Stable event IDs support provider deduplication.

**Notification provider** — A destination adapter such as console, Windows desktop, Discord, generic webhook, NDJSON, or an explicitly enabled command hook.

**Privacy mode** — A rule controlling which fields a notification may reveal, from generic new-mail text through sender, subject, or a bounded sanitized preview.

**Quiet hours** — A local schedule that suppresses, defers, or digests otherwise matching notifications.

**Watcher** — The durable background process that checks selected folders, advances checkpoints, suppresses duplicates, applies rules, and dispatches providers.

**Webhook** — An outbound HTTPS request sent by the local watcher to another application. It is not an inbound callback made by Exchange to this terminal client.

## Safety terms

**Capability** — A combined status covering implementation path, server-version eligibility, authenticated account permission, verification evidence, and safety class.

**Destructive** — An operation that makes recovery difficult or impossible, such as permanent deletion. It needs narrow scope and high-friction confirmation.

**Mutating** — An operation that changes mailbox or durable local data. Not every mutation is destructive; saving a draft is mutating but recoverable.

**Synthetic event** — A clearly fake notification or item used to test local behavior without reading or changing real mailbox content.

**Undo Send** — A local delay before actual transmission. It allows cancellation during the delay but cannot recall a delivered message.
