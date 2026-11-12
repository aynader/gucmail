# Privacy and security

## Quick explanation

GUC Mail CLI connects directly to the configured HTTPS Exchange Web Services endpoint. It is an
independent, unofficial client. Mailbox content stays local unless you explicitly send mail,
configure an outbound webhook, or enable another integration.

## Credentials

Credential sources are considered in this order: an intentionally supplied value, an environment
value, the operating-system keyring, a secure hidden prompt, then the legacy `creds.env`
compatibility path. Avoid command-line passwords because process listings and shell history can
expose them.

The normal TOML configuration contains account identity and preferences but never a password.
`gucmail logout` removes the selected keyring credential; it does not delete mail, local state, or
backups. A legacy credential file is parsed as data and never executed. Remove it only after secure
login works.

TLS certificate verification is enabled. The configured endpoint must be HTTPS and may not contain
URL credentials, parameters, a query string, or a fragment. There is no normal insecure-TLS switch.
Redacted status/settings output hides signatures, editor arguments, local paths, custom folder
names, and custom endpoint paths; use the dedicated configuration commands when you intentionally
need local details.

## Hostile email and terminal output

Subjects, display names, bodies, headers, and filenames are untrusted. The renderer strips ANSI and
terminal control sequences. HTML is converted to inert text; scripts, frames, forms, remote images,
tracking pixels, and other remote resources are not loaded automatically.

URLs are displayed as text. Opening one is a separate action and should be reviewed first.

## Attachments and external tools

Attachments are not downloaded while listing metadata. Saving uses filename normalization, path
containment, collision handling, declared and observed size limits, and warnings for risky file
types. The default maximum is 52,428,800 bytes (50 MiB). Files are never executed or opened
automatically.

Optional attachment text extraction is local, bounded, and disabled unless configured. It does not
run macros, scripts, links, or OCR. Supported safe formats can require the `attachments` optional
dependency. Password-protected or suspicious files are skipped.

Command hooks are launched as argument lists with no command shell. Command
hooks are disabled by default and receive an allowlisted, sanitized JSON payload through standard
input.

## Local data

Platform-specific paths are shown by `gucmail config path`. Local state may include item-reference
mappings, scheduler jobs, saved searches, checkpoints, notification history, and draft recovery.
Message-body caching is off by default. Attachment indexing, semantic search, monitoring, and
command hooks are also off by default.

Cache, index, archive, and backup are different:

- **Cache:** disposable acceleration data.
- **Index:** searchable derived text that can be rebuilt or erased.
- **Archive/offline view:** a local reading mode over a completed backup.
- **Backup:** a self-describing private copy intended for recovery.

Backups can contain complete private messages and attachments. Encrypt copies stored on removable,
shared, or cloud-synchronized media. Keep the passphrase separately; the application does not store
it in plaintext.

## Logs and diagnostics

Normal logs exclude passwords, authorization headers, cookies, message bodies, attachment data, and
raw SOAP. Email-address redaction is enabled by default. Debug output can still contain operational
metadata, so review a diagnostic bundle before sharing it.

## Webhooks and notifications

Webhook support means the local monitor sends an outbound request after detecting an event; it does
not mean Exchange can call a local computer. Payload fields are allowlisted and redacted by default.
Secret headers should use the keyring. HMAC signing lets the receiver verify the body. Redirects are
not followed, and private-network targets require an explicit opt-in. Notification delivery claims
are atomic across watcher processes, and append-only outcome detail is redacted before persistence.

## Destructive and uncertain operations

Mutating commands require confirmation unless a documented automation flag is supplied. Permanent
deletion and privileged SOAP operations use higher-friction expert controls. A network failure after
a send may mean Exchange accepted the message. The state becomes `send_state_uncertain`; do not
retry manually until Sent Items reconciliation or manual review resolves it.

## Live-test safety

Normal tests use synthetic data and fakes. Live checks are explicitly enabled, authenticate once,
use small bounded read-only requests, do not mark mail read, do not fetch arbitrary bodies or
attachments, and report their request count. Sending, scheduling, moving, deleting, flagging, and
other live mutations are excluded unless separately and explicitly authorized.

Optional per-image OCR uses a separately previewed, explicitly enabled local operation.
It uses only an already installed Windows language and one selected bounded PNG/JPEG,
with an owned child and temporary copy cleanup. No language/model download, mailbox
attachment fetch, cloud recognizer or automatic indexing occurs. OCR may be inaccurate;
review the labelled recognized text. Temporary cleanup does not claim forensic erasure.

The separate attachment index producer requires a saved current-machine policy and a
confirmed bounded build/rebuild before downloading selected content. Formats, per-file
input/parser/output limits and per-message/run file/byte budgets apply. Image files need
additional saved OCR consent and an installed language. No engine or parser is installed.
Extracted text, sanitized filenames and outcomes are private derived SQLite data; ordinary
and OCR results are labelled, with OCR uncertainty/truncation exposed in local search.
Disabling stops future extraction; confirmed index clear removes existing derived content
without guaranteeing erasure from snapshots. No watcher or startup indexing is added.
