# Troubleshooting by symptom

Start with the visible symptom. Commands below are diagnostic unless marked otherwise. `gucmail
doctor` does not authenticate unless authentication is intentionally requested or an existing
credential is being tested.

## `gucmail` is not recognized

1. Confirm that installation completed in the same environment you are using.
2. On Windows, try `py -3.11 -m gucmail --help`.
3. For a repository virtual environment, try `.\.venv\Scripts\gucmail.exe --help`.
4. With pipx, run `pipx list` and ensure the pipx binary directory is on `PATH`.

## Login fails or repeatedly prompts

Run `gucmail status`, then `gucmail doctor`. Confirm the email, username, HTTPS endpoint, keyboard
layout, and system clock. A changed password invalidates the stored keyring entry; run `gucmail
logout`, then `gucmail login` once. Do not retry rapidly. An authentication failure uses exit code
`4`.

## Keyring is unavailable

Use the secure prompt for the current run. Do not put the password in `config.toml` or a shell
command. `gucmail doctor` reports whether the OS keyring backend can be used.

## TLS, DNS, timeout, or throttling error

Check network access and the endpoint with `gucmail doctor`. Keep TLS verification enabled. A network
failure uses exit code `5`. If Exchange supplies a back-off duration, wait for it; the client applies
bounded exponential backoff and jitter. Do not start several watchers to work around throttling.

## A feature says unsupported or unauthorized

Run:

```text
gucmail ews capabilities
gucmail ews describe OPERATION
```

`unsupported_server_version`, `disabled_by_server_configuration`, `unauthorized_for_account`, and
`administrative_only` are different outcomes. No live probe means `not_probed` or
`implemented_not_live_verified`, not success.

## A message or folder reference is stale

Moving or updating an Exchange item can change its server ID or change key. Refresh or list its
folder again. Do not paste raw EWS IDs into ordinary workflows. Exit code `6` means not found; `7`
means a conflict such as a changed item.

## Attachment will not save

Check the reported size against the default 50 MiB limit, free disk space, destination permissions,
and filename collision. Dangerous extensions require acknowledgment. A path outside the selected
download directory is rejected. Item attachments may have different server limitations from files.

## A draft disappeared after a crash

Run `gucmail drafts recover`. While the composer is open, recovery journals are written atomically
every 30 seconds by default and once more on cancel. A server/local conflict is not overwritten
automatically; inspect `gucmail drafts conflicts`.

## A scheduled message did not send

Run:

```text
gucmail scheduler status
gucmail scheduled list
gucmail scheduler doctor
```

The local strategy needs the computer awake, network access, valid credentials, and periodic
`scheduler run-once` execution. Missed jobs are considered on the next run. `waiting_connectivity`
and `waiting_authentication` can retry conservatively; `send_state_uncertain` requires review and is
not retried blindly. See [Scheduling](../scheduling/index.md).

## Backup verify fails

Stop using that copy for restore. Keep the directory unchanged, run `gucmail backup inspect`, and
compare the manifest, object counts, and cryptographic hashes. An encrypted backup needs the same
passphrase used at creation. A restore always starts with a plan; never bypass integrity failure.

## Search returns too few or stale results

Check the selected source: live server, cache, search index, backup, or local. Run `gucmail index
status`, then update or verify the index. Rebuild only if verification reports damage. Fuzzy search
uses a default threshold of `82`; lower thresholds increase noise. Semantic search is optional and
off by default.

## Cache or database is locked/corrupt

Close duplicate client or scheduler processes and retry. `gucmail cache status` reports health.
Clearing the disposable cache must not remove scheduled jobs, recovery drafts, indexes, or backups.
Use exit code `11` details to identify the affected local store.

## Webhook delivery fails

First use `gucmail webhooks test NAME` for a network-free synthetic preview, then use
`gucmail --yes webhooks test NAME --deliver` for one outbound test. Confirm HTTPS, method, timeout,
keyring secret, field allowlist, and receiver HMAC configuration. Redirects are not followed. HTTP
`408`, `409`, `425`, `429`, `500`, `502`, `503`, and `504` can retry within the configured limit.

## The TUI layout is unreadable

Increase terminal size or use the routed narrow layout. Try `--no-color`, set `no_color = true`, or
choose a Unicode-capable font. `?` opens context help and the active shortcut list.

## Safe diagnostics to share

Use `gucmail diagnostics` or the sanitized bundle option if available. Review the output before
sharing. It should contain versions, path health, capability status, and redacted errors—not
passwords, cookies, headers, bodies, attachments, or raw personal addresses.
