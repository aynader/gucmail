# Compose, reply, and forward safely

The composer supports To, Cc, Bcc, subject, plain text or HTML source, importance, receipt requests, and validated local attachments. Reply and reply-all begin from a fresh preview so recipients can be reviewed instead of silently inherited.

## Saved defaults

Open **Settings → Composer & Drafts** to choose the format and importance for new messages, replies, and forwards. You can change those values in each composer. Saved drafts, duplicates, and recovered drafts keep their existing format, importance, and receipt choices.

- **Default body format** starts new composers in plain text or HTML source. Plain text is the default. The editor displays HTML as text and preserves your supplied HTML for delivery. It does not execute HTML or load remote resources. Generated replies and signatures escape plain text; converting to plain text removes active content and remote-resource markup.
- **Default importance** starts new compositions at Normal, High, or Low.
- **Request read receipt** and **Request delivery receipt** start unchecked. A request does not guarantee a receipt; server and recipient policies control the response.
- **Default signature** stores your signature text. Enable **Insert signature automatically** to add it to a new composition, above the quoted history in generated replies and forwards. Saved drafts keep the signature already present. Signature text is escaped in HTML and excluded from settings exports and redacted status.
- **Missing-attachment warning** checks wording in your subject and new body text. It ignores quoted history. Disabling it still leaves attachment existence, integrity, and size checks active.
- **Recipient-count warning** warns at 20 recipients by default. Set a different threshold, or zero to disable this warning. The hard limit remains 500 recipients.
- **Empty-subject behavior** requires a subject by default. Choose **Warn** to allow an empty subject after the final send review. The body is still required.

On the CLI, `send` and `drafts create` use these defaults. `--body-format plain`, `--importance low`, `--no-read-receipt`, `--no-delivery-receipt`, and `--no-signature` override them for one composition. Reply, reply-all, and forward accept the same overrides; their `--body` text is plain text and is escaped when the generated response uses HTML. For `send` and `drafts create`, `--body` and `--body-file` contain the selected format. Use `send --dry-run` to inspect the exact result locally.

## Reply, forward and Sent copies

In **Settings → Mail**, **Default reply recipients** chooses sender or all visible
recipients for ordinary Reply. Explicit Reply all still uses all and excludes your own
address, duplicate addresses and Bcc. Review every recipient before sending. Separate
**Quote original text in replies**, **Quote original text in reply all**, and **Include
original text in forwards** choices affect new responses. A saved response keeps its text.
Turning off forwarded text does not remove inherited Exchange attachments; the send review
discloses them separately.

**Save Sent Items copies by default** starts enabled. The composer offers a **Sent copy:
on/off** checkbox for each message. The final review shows the choice and warns when it is
off. Local drafts, recovery, scheduling and Undo Send preserve that reviewed value. A later
default change does not change an existing draft or due job. Server-only draft imports start
with Sent copying enabled; existing local choices survive server edits and conflict resolution.

CLI `reply --reply-mode sender` overrides the recipient default. Reply, reply-all and forward
accept `--include-history` or `--no-history`. `send`, responses and `drafts create` accept
`--save-sent-copy` or `--no-sent-copy`; `drafts edit` can revise the saved choice.

## Before sending

- Resolve ambiguous recipients and remove duplicates.
- Review every To, Cc, and Bcc address; reply-all excludes the current account where possible.
- Check empty subject/body warnings and missing-attachment wording.
- Confirm that attachment paths still exist and their content has not changed.
- Review To/Cc/Bcc, subject, format, importance, receipt requests, Sent-copy choice, files, and warnings in the confirmation step.

Use a body file or a configured external editor only through an argument list, never a shell command assembled from message text. A signature is private configuration and is not shown in redacted status output.

The send review binds the message choices and local attachment contents. If the message or files change after review, submission stops and the recovery copy remains available for a new review. CLI draft-send previews are local; adding `--schedule` to a dry run does not create a queued job. Scheduling a draft requires confirmation.

## Delivery outcomes

A definite failure before transmission can be retried conservatively. A timeout after the request may mean Exchange accepted the message; that outcome becomes uncertain and must not be sent again blindly. Inspect local attempt history and, when requested, Sent Items. If you disabled the Sent copy, establish delivery manually before intentionally sending another composition. See [Sending and uncertain-result problems](help:troubleshooting.sending) and [Drafts and recovery](help:mail.drafts).
