# Tutorial: read and prepare a first message

This walkthrough lets you practice without changing a real mailbox item. Use synthetic text and cancel before transmission until you understand the confirmation screen.

## Read safely

1. Open Inbox and select a message summary.
2. Open the reader. Confirm that remote content remains blocked.
3. Review sender, recipients, time, folder, attachment metadata, and the sanitized body.
4. Do not choose Mark read unless you intend to change that mailbox item.

The command equivalents are `gucmail messages list --folder inbox --limit 5` and `gucmail messages show MESSAGE_REFERENCE`. Listing remains bounded; showing a message does not intentionally mark it read.

## Prepare a synthetic message

Open Compose and use `learner@example.invalid` while practicing. Enter a synthetic subject and body, review recipient count and attachments, then cancel. If you later perform a real send, use only a recipient you control and confirm the final recipient list. A request accepted by Exchange is not the same as guaranteed delivery. See [Compose safely](help:mail.compose).
