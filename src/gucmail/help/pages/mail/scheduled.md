# Scheduled mail, Outbox, and Undo Send

The default strategy is a durable local scheduler. A future message remains local until a worker claims the due job and asks Exchange to send it. Native server-delayed delivery is not assumed merely because an extended property exists.

## States and reliability

The Outbox can show scheduled, undo delay, waiting for connectivity, waiting for authentication, sending, retry scheduled, sent, failed, cancelled, uncertain, or manual review. Jobs, attachment hashes, attempts, leases, and transition history survive a normal restart.

Multiple workers use transactional claims. A known pre-request temporary failure can retry with bounded backoff. An ambiguous post-request failure is never repeated automatically. Changed or missing attachments also stop unattended sending.

## Time and sleep

Store an explicit offset or review the resolved time zone. If the computer sleeps, local sending pauses. Closing the app is safe when the background task is on (`gucmail notifications on`) or another scheduler task runs. Overdue work is evaluated on the next run according to policy. Undo Send is a local countdown, not recall after Exchange accepts a message. Read [Does my PC need to stay on?](help:faq.pc-on).
