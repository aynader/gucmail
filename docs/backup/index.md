# Backups

A backup is a private copy of your mail on your computer. You can read and search it without
signing in, and restore it to your mailbox later. Making a backup never changes your mail and never
marks anything read.

A backup is not the cache or the search index. Those are temporary and can be deleted any time.

## Make a backup

In the app, use **Ctrl+K → Open backup center**. Or from the terminal

```text
gucmail backup estimate --folder inbox
gucmail backup create --folder inbox --encrypt
```

`estimate` shows how much will be downloaded, first. `--encrypt` asks for a passphrase. Keep it
safe, because nobody can open the backup without it. Leave out `--folder` to back up everything.

Next time, save only what changed.

```text
gucmail backup incremental BACKUP_ID --encrypted
```

If a backup stops halfway, continue it.

```text
gucmail backup resume BACKUP_ID
```

## Check a backup

```text
gucmail backup list
gucmail backup verify BACKUP_ID
```

`verify` checks every file against its fingerprint. Don't restore from a backup that fails.

## Read it offline

```text
gucmail backup browse BACKUP_ID --limit 50
gucmail backup search BACKUP_ID "exam schedule" --fuzzy
gucmail backup export BACKUP_ID --output my-mail.gucmail.zip
```

None of these connect to the server. `export` makes one file you can copy elsewhere.

## Restore

Restore always shows a plan first, with exact counts.

```text
gucmail backup restore-plan BACKUP_ID --destination "Restored mail"
gucmail --yes backup restore BACKUP_ID --destination "Restored mail" --apply
```

Restored mail goes into its own folder. Nothing is overwritten, and restored drafts are never sent.
If a restore is interrupted, look in that folder before trying again, because some items may
already be there. `gucmail backup restore-status` shows what happened.

A restore recreates your mail, not an exact clone. Things like original delivery times and
server IDs can't be brought back.

## Clean up old backups

```text
gucmail backup prune --keep-last 3
gucmail backup prune --keep-last 3 --apply
```

Without `--apply` it only shows what it would remove. Removed backups go to a trash folder first.
Backups that newer ones depend on are kept.

## Privacy

Backups contain your mail. Encrypt them if they leave your computer, and delete ones you no
longer need.
