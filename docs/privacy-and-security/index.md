# Privacy and security

gucmail talks only to the GUC mail server, over HTTPS. Your mail stays between that server and your
computer unless you send it, or set up a webhook or Discord alert yourself. gucmail is independent
and not made by GUC or Microsoft.

## Your password

Your password is kept in your system's password store (Windows Credential Manager on Windows),
never in a settings file. `gucmail logout` removes it.

Never type your password on the command line, where it can end up in your command history.

If you still have a `creds.env` file from older versions, gucmail reads it as plain text and never
runs it. Sign in once with `gucmail login`, then delete the file.

## Mail is treated as untrusted

- Remote images, tracking pixels and scripts never load. Formatted mail is shown as plain text.
- Links show as text. Nothing opens until you choose to open it.
- Odd characters that could mess up your terminal are removed from subjects, names and filenames.
- Attachments are never opened or run. Saving checks the filename, refuses to write outside the
  download folder, and warns about risky types like `.exe`. The size limit is 50 MiB by default.

## What's kept on your computer

`gucmail config path` shows where. The table lists what's there.

| What | Is it a backup? | Delete with |
|---|---|---|
| Cache. Recent message lists so the app opens fast | No | `gucmail cache clear` |
| Search index. Optional, off until you build it | No | `gucmail index clear` |
| Drafts, scheduled mail and settings | No | Delete in the app |
| Backups. Full copies you made | Yes | `gucmail backup prune` |

Message text isn't cached by default. Attachment indexing, search by meaning and alerts are off
until you turn them on.

Encrypt backups that leave your computer, and keep the passphrase somewhere safe.

## Logs

Logs never contain passwords, message text or attachments. Email addresses are hidden by default.
Read `gucmail diagnostics` output before sharing it.

## Alerts that leave your computer

Webhook and Discord alerts send only the fields you allow, over HTTPS. Keep webhook links private,
since anyone with one can post to it. gucmail can sign each alert so your receiver can check it
came from you.

## Before anything changes

gucmail asks before sending, deleting or changing mail. Delete moves mail to Deleted Items, where
you can get it back. Deleting for good needs an extra confirmation.

If a send might have gone through when the connection dropped, gucmail marks it **uncertain** and
never sends it again by itself.

## More detail

- [Where gucmail keeps data](local-data-locations-and-retention.md)
- [Search and indexing](../search/index.md)
- [Backups](../backup/index.md)
