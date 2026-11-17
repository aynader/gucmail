# Troubleshooting

Find your problem below. `gucmail doctor` is always a safe first step. It checks your setup
without signing in or touching your mail.

## `gucmail` is not recognized

Open a new PowerShell window. If it still fails, run `py -3.11 -m pipx ensurepath`, open another
new window and try again. `py -3.11 -m gucmail` also works.

## Your password isn't accepted

Press F5 in the app to sign in again, or run `gucmail login`. If you changed your GUC password,
this is expected. Don't retry many times quickly. The server may block you for a while.

## Can't reach the server

Check your internet connection, then press F5 once. `gucmail doctor --network` tests the
connection to the GUC mail server without signing in. If the server is busy, gucmail waits and
tries again by itself.

## "The GUC server does not permit…"

The GUC server doesn't offer that feature to your account. Nothing was changed. The
[support status](../features/support-status.md) page lists what works.

## "That message changed or moved"

Someone (you, in Outlook or on your phone) changed it since gucmail last looked. Press F5 and try
again.

## An attachment won't save

Check free disk space and that you can write to the download folder. Files over 50 MiB are
refused by default. Risky file types, like `.exe`, ask before saving.

## A draft disappeared after a crash

```text
gucmail drafts recover
```

Drafts save every 30 seconds while you type. If your draft changed somewhere else too, gucmail keeps
both and asks. `gucmail drafts conflicts` lists those.

## Scheduled mail didn't send

```text
gucmail scheduled list
gucmail scheduler status
```

Your computer must be on, awake and online at the send time, with alerts or the scheduler task
installed. A message marked **uncertain** may have gone already. Check **Sent** before sending it
again. See [Scheduled mail](../scheduling/index.md).

## New-mail alerts stopped

```text
gucmail notifications status
```

If the task isn't running, turn alerts off and on again with `gucmail notifications off` and
`gucmail notifications on`.

## Search misses mail

Each result says whether it came from the server, the local index or a backup. If you use the
local index, run `gucmail index update`. Try `--fuzzy` for typos.

## A backup fails its check

Don't restore from it. Run `gucmail backup inspect` to see what's wrong, and make a new backup.
Encrypted backups need the passphrase you chose when making them.

## "Local data is locked" or "needs repair"

Close other gucmail windows and background commands, then try again. `gucmail cache status`
shows what's wrong. Clearing the cache never removes drafts, scheduled mail or backups.

## A webhook doesn't arrive

```text
gucmail webhooks test NAME
gucmail --yes webhooks test NAME --deliver
```

The first shows what would be sent. The second sends one test. Webhooks must use HTTPS.

## The screen is hard to read

Make the window wider, or try another theme in Settings. `gucmail --no-color` turns colours off.

## Asking for help

`gucmail diagnostics` prints versions and setup details with addresses and passwords removed.
Read it before sharing. Never share your password, `creds.env` or message content.
