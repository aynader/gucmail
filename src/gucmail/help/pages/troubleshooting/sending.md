# Sending, draft, and uncertain-result problems

Sending failures fall into different safety classes. The recovery action depends on whether the request definitely remained local or may have reached Exchange.

## Before transmission

Recipient validation, empty-content policy, missing attachments, changed files, size limits, confirmation scope, or authentication can stop a message before sending. Correct the reported field and reuse the same draft. A local draft revision conflict needs comparison, not overwrite.

## During or after transmission

A definite temporary failure before request transmission can use bounded retry. A timeout, disconnect, or malformed result after bytes may have crossed the network becomes uncertain. Never click Send again blindly. Inspect Sent Items and the local correlation/attempt history; reconcile or move the job to manual review.

Scheduled jobs may wait for connectivity or authentication. Closing the TUI does not run a scheduler. Changed attachments stop unattended send. If the interface keeps loading, cancellation must return control and [Connection troubleshooting](help:troubleshooting.connection) applies. Recovery journals can be inspected with drafts recovery; they never prove Exchange received a message.
