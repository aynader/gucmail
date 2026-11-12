# Glossary

**Archive** — A server folder or a local offline collection kept for long-term access. It is not
automatically a verified backup.

**Backup** — A self-describing private copy with a manifest and cryptographic integrity checks.

**Cache** — Disposable local data that makes repeated operations faster. Exchange remains the
source of truth.

**Capability** — A feature or EWS operation together with its implementation, server, account,
verification, and safety status.

**Change key** — Exchange's version token for an item or folder. A stale token can cause a conflict.

**Cluster** — A locally computed group of related messages, such as a sender, conversation, topic,
time period, or attachment type.

**Destructive** — An operation that can make recovery difficult or impossible, such as permanent
deletion.

**Draft** — A message saved for editing and not yet sent. It can be local, on Exchange, synchronized,
or conflicted.

**EWS** — Exchange Web Services, the HTTPS/SOAP interface used to communicate with Exchange.

**Exchange** — Microsoft's mail, calendar, contact, and task server product.

**Fixture tested** — Tested with sanitized synthetic request/response data, not a live mailbox.

**FTS** — Full-text search. GUC Mail CLI uses a local searchable text index when enabled.

**HMAC** — A keyed signature over a webhook body. The receiver can use it to verify authenticity and
integrity.

**Index** — Derived local search data. It can be rebuilt and is not a backup.

**Item reference** — A short local handle for a message, event, contact, task, or draft. It hides
long and changeable Exchange IDs.

**Mutating** — An operation that changes mailbox or local durable data but is not necessarily
destructive.

**NDJSON** — Newline-delimited JSON: one JSON object per line, suitable for streaming tools.

**Outbox** — The view of locally delayed, scheduled, retrying, failed, uncertain, or completed send
jobs.

**Quota** — A server-defined mailbox storage limit. If the server does not return exact values, the
client reports that quota is unavailable.

**Scheduled message** — A draft plus a durable instruction to attempt sending at a future instant.

**Semantic search** — Optional local similarity search using embeddings. It is disabled by default
and does not send mailbox text to a cloud service.

**SOAP** — The XML message format used by EWS. This release exposes typed SOAP adapters only;
arbitrary raw SOAP execution is not available. Ordinary
commands use typed operations.

**Synchronization** — Incrementally recording Exchange changes from a checkpoint instead of
downloading the whole mailbox repeatedly.

**TUI** — Terminal user interface: the full-screen keyboard-driven application opened by `gucmail
tui`.

**Undo Send** — A local countdown before transmission. It is not recall of a message already accepted
by Exchange.

**Webhook** — An outbound HTTP request sent by the local monitor to a configured service after an
event. It is not an inbound public server.
