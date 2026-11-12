# Mailbox storage and Exchange thresholds

Exchange does not necessarily give a mailbox one simple capacity. It can enforce three separate
thresholds:

- **Issue Warning**: Exchange warns about storage pressure. Sending and receiving normally continue.
- **Prohibit Send**: Exchange blocks outgoing mail. Incoming mail may continue.
- **Prohibit Send & Receive**: Exchange blocks both directions.

GUC Mail keeps these values separate. Each value carries its source and confidence. A value can be
server-reported, automatically detected, observed in an Exchange notification, user-configured, the
known GUC fallback, estimated, or unknown.

## What the known 92 MB value means

GUC Exchange quota notices have shown a **92 MB Prohibit Send threshold**. GUC Mail can use that as
a configurable fallback when the server does not return a stronger value. It does not mean that the
mailbox has 92 MB of total capacity, and it is never copied into the Send & Receive threshold.

For example, a notice might show approximately 97 MB in the mailbox when sending was blocked. That
97 MB value describes usage at the time of the notice. It is not a quota, and it is not reused as
current usage. Current mailbox usage must be retrieved or estimated independently.

For that example, GUC Mail can accurately say:

```text
Send quota [##########] 105.43%  97 MB/92 MB · SEND BLOCKED
Receiving: not known to be blocked
```

The percentage compares usage with the threshold at which sending is blocked. It is allowed to
exceed 100%. It does **not** describe percentage of a known total mailbox capacity.

## Partial and unavailable information

Unknown values stay unknown. Useful partial states include:

```text
Mailbox 74 MB used · limits unknown
Send limit 92 MB · usage unavailable
```

Only when neither usage nor any threshold is available does the application report mailbox storage
as unavailable. A folder-size estimate is labelled as an estimate because recoverable, hidden, or
otherwise inaccessible data may not be included.

An observed send block never implies a receive block. If the Send & Receive threshold is unknown,
the status says that receiving is **not known to be blocked**.

## Quota notices

GUC Mail can conservatively recognize structured, system-generated Exchange quota notices. It stores
only the parsed event, historical usage, relevant threshold, time, source, and confidence. It does
not treat ordinary email text as quota evidence, does not repeatedly scan the mailbox, and never
deletes a warning message automatically.

## Mailbox storage versus local storage

Server mailbox usage is separate from the application's local cache, search index, backups, and
download directory. Local disk capacity is reported separately and is never added to or substituted
for Exchange mailbox usage.

Storage cleanup is plan-based. Inspect candidates and expected reclaimable space first; cleanup does
not permanently delete mailbox data by default.

