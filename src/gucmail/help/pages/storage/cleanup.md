# Analyze mailbox cleanup without surprise deletion

Cleanup begins with a bounded analysis, never a mass mutation. Candidate groups can include large messages, large attachments, exact duplicates, old unread mail, newsletters, or selected folders.

## Plan before action

A plan records query scope, limits, exclusions, candidate references, exact count, estimated reclaimable bytes, creation time, and a token tied to that content. Review representative messages and remove false positives. Re-running an analysis creates a new plan rather than silently changing the old one.

Execution verifies the current token and confirmation scope. Ordinary cleanup moves items to Deleted Items by default. Permanent deletion is a distinct, higher-risk operation and is never inferred from “free some space.” Partial results need per-item status and recovery guidance.

Duplicate analysis must preserve at least one canonical copy and explain why candidates match. Unanswered-mail or conversation claims require complete enough data; otherwise the report should say that analysis is unavailable. Consider a verified [backup](help:backup.overview) before a large cleanup.
