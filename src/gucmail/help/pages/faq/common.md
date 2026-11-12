# Common questions about GUC Mail CLI

These answers describe the installed terminal client. Server-dependent features still show a capability label for the current account, and local background work depends on the computer that runs it.

## Notifications and scheduled mail

**Do I need to keep the TUI open for notifications?** No. The watcher is a separate headless process. It must be running, connected, and able to unlock credentials; closing the TUI does not stop a correctly supervised watcher.

**Does my computer need to stay on?** The computer running the watcher or scheduler must be awake and online. A sleeping laptop cannot detect mail or send a locally scheduled message. An always-on private server can do that work while the laptop is off. Read [Does my PC need to stay on?](help:faq.pc-on).

**Can I receive notifications on Discord?** Yes, when a configured Discord-compatible provider and routing rule are enabled. The webhook URL is a secret. Start with a synthetic test and the most private payload mode.

**Can GUC Mail send email later?** Yes. The durable local scheduler stores a reviewed draft and due time. If native Exchange delayed delivery is unavailable, the local worker sends it when due. An asleep or stopped worker sends nothing until it resumes.

**What is Undo Send?** A short local delay before transmission. Cancel during that window to return the message to an editable state. It is not server recall after delivery.

## Storage, backup, search, and offline use

**What happens if my mailbox is full?** Exchange may warn, block sending, or eventually block receiving at different thresholds. Open Storage to distinguish current usage, server evidence, and configured fallback values. The Prohibit Send threshold is not automatically total capacity.

**What is the mailbox capacity?** Unknown unless Exchange reports it. The known GUC fallback is a 92 MiB sending threshold, not total capacity. Warning and receiving thresholds remain independently unknown until established.

**Where are backups stored?** In the destination chosen for that backup. The backup center shows its exact local path, verification state, encryption metadata, and parent chain. A cache or search index is not a backup.

**Can I use GUC Mail offline?** You can browse verified backups and suitable local indexes, inspect drafts and schedules, change local settings, and read Help. Current Exchange mailbox truth, sending, and server mutations require a connection.

**Can I search inside attachments?** Only when attachment indexing is explicitly enabled and the installed safe extractor supports that format. Indexing is bounded by size and type. Unsupported, encrypted, or dangerous files remain metadata-only.

## Privacy, credentials, and recovery

**Does GUC Mail upload mail to an AI service?** No. Help search, fuzzy search, clustering, and natural-language commands are deterministic local features. Outbound notification providers receive only the fields allowed by their privacy and routing configuration.

**Where is my password stored?** Prefer the operating-system keyring. Normal configuration never stores a plaintext password. `creds.env` is a legacy development fallback and should be protected and replaced with keyring storage.

**Can I recover a deleted email?** Normal deletion moves mail to Deleted Items when the server permits it. Restore is usually possible while that item remains there. Permanent deletion and server retention limits can make recovery impossible.

**Why is mail not refreshing?** Check the visible connection state, then run one Doctor pass. Common causes include sleep/resume, network changes, an expired session, changed credentials, server backoff, or a stopped watcher. Avoid rapid repeated login attempts.

**How do I reset the interface?** Open Settings, search for layout, preview the reset, and apply it. A layout reset does not delete mail, drafts, schedules, backups, or credentials.

## Useful checks

```text
gucmail status
gucmail scheduler status
gucmail headless health
gucmail notifications history
gucmail backup list
gucmail index status
```

These commands are read-only. Continue with [Doctor](help:diagnostics.doctor), [notification troubleshooting](help:troubleshooting.notifications), or [backup and search troubleshooting](help:troubleshooting.backup-search) for recovery steps.
