# What gucmail does

The app and the `gucmail` commands do the same things, with the same safety checks. What works
also depends on what the GUC server allows. [Support status](support-status.md) lists it.

## Mail

```text
gucmail folders tree
gucmail messages list --folder inbox --limit 25
gucmail messages show MESSAGE_REFERENCE
gucmail attachments list MESSAGE_REFERENCE
```

The list loads a page of headers. Message text and attachments download only when you open them.
Formatted mail is shown as plain text, without remote images or scripts.

`MESSAGE_REFERENCE` is a short name gucmail gives each message, so you never copy long server IDs.

Folders show their Outlook names, even when your mailbox uses another language.

## Writing

gucmail checks recipients, attachments and empty subjects before sending, and warns when you
mention an attachment but didn't add one. Sending always shows a review first. Reply all lists
every recipient.

Drafts save as you type. See the [user guide](../user-guide/index.md) and
[scheduled mail](../scheduling/index.md).

## Changing mail

Mark read, flag, categorize, move, copy and delete all change mail on the server. Delete moves mail
to Deleted Items, where you can get it back. Deleting for good is never the default and always
asks twice.

## Storage and tidying up

```text
gucmail quota show
gucmail insights storage
gucmail insights senders
gucmail insights duplicates
gucmail insights unanswered --self-address me@example.test
gucmail cleanup analyze
```

[Mailbox storage](storage-and-quota.md) explains the numbers. Cleanup always shows a plan with
exact counts and the space you'd free, and moves mail to Deleted Items rather than deleting it for
good.

## On your computer only

Saved searches, drafts, scheduled mail, webhooks and clusters are stored on your computer. Deleting
gucmail's data folder never deletes server mail, but it does lose scheduled mail and drafts. See
[where gucmail keeps data](../privacy-and-security/local-data-locations-and-retention.md).

## Basic, Advanced and Expert

**Settings → Advanced → Settings complexity** changes how much the app shows.

- **Basic** covers mail, attachments, search, drafts, scheduling, calendar, contacts and storage.
- **Advanced** adds folders, filters, backups, alerts, rules, indexing and cleanup.
- **Expert** adds server details and diagnostics.

It only changes what's shown, never what your mail does.

## Good to know

- "Unsupported" means the GUC server doesn't offer it. Nothing changed.
- If a message changed elsewhere, press F5 and try again.
- If a bulk action stops partway, it lists what finished. Exit code 10 means some items failed.
