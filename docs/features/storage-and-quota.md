# Mailbox storage

The header shows how much of your mailbox you use.

```text
Mailbox 74 MiB of 92 MiB (81%)
```

## Where the numbers come from

**Used** is the sum of your folder sizes, read from the GUC server. Hidden system folders
add a few MiB that this does not count. gucmail checks again every 10 minutes.

**92 MiB** is the size at which GUC stops you from sending mail. The server doesn't
share its limits with mailbox users, so this number comes from GUC's own quota warning
emails. You can change it in Settings, under Storage.

Over the limit, you can still receive mail, but sending stops until you free space.
The percentage can go above 100%.

## Exchange limits

Exchange can set three separate limits.

- **Warning**. You get a warning email.
- **Send blocked**. You can't send. You still receive.
- **Send and receive blocked**. Mail stops in both directions.

gucmail keeps these apart. A send block never implies a receive block. When a limit is
unknown, gucmail says so instead of guessing.

## Quota warning emails

gucmail can read Exchange's own quota warning emails to learn your limit. It keeps only
the numbers and the date, never deletes the email, and ignores ordinary mail.

## Local storage

The cache, search index and backups on your computer are counted separately. They never
count toward your mailbox.

```text
gucmail quota
```
