# Command palette and natural-language requests

The command palette is a discovery surface over the same canonical actions used by the rest of the application. It can search by title, category, keyword, alias, shortcut, and a deterministic natural-language interpretation.

## Review before execution

A useful result explains the selected command, extracted values, current context, availability, and safety class. Low-confidence or incomplete requests should ask for clarification or become a harmless search. A destructive word must never be guessed into an executable deletion.

Examples such as “find unread mail from Ahmed Example” can produce a search preview with sender and unread slots. Minor spelling mistakes should still discover Search Mail. A request to permanently delete needs an exact scope and high-friction confirmation.

The preview uses concrete labels for interpreted values. For example, a mail search identifies its sender, inclusive date span, read state, and folder or **All Mail** scope. Scheduling shows the resolved local date, 24-hour time, time zone, and send action; `morning` means 08:00 in the configured time zone.

When an interpreted command can change local or server data and has parameters, Enter opens an editable parameter review before the normal confirmation. Dates, sizes, people, folders, booleans, lists, and other declared parameter types are parsed and validated again after editing. Invalid or unsafe values stay on the review screen with an explanation. Read-only commands do not acquire an unnecessary confirmation, and cancelling the review executes nothing.

## Bounded follow-ups

The palette accepts three deliberately narrow result handoffs: a filtered message search followed by a read-state change, a size-filtered message search followed by a selected-message backup, and an attachment search followed by a safe bulk download. It does not split arbitrary pipelines.

Entering one of these sequences runs only the bounded read-only search. A separate review shows the real matched count, known and unknown sizes, exact result rows, and any truncation. Read-state changes require another exact-target confirmation. Backup opens Backup Center with the result references and an editable destination, where creation has its normal confirmation. Attachment download requires a reviewed destination and a second confirmation; unknown-size, over-limit, and over-500-MiB groups are blocked, existing files are renamed rather than overwritten, and downloaded files are never opened automatically.

The palette works locally and does not send text to an external language model. Some proposed commands can be unavailable offline or unsupported by the current server; their result explains why rather than pretending success. Press Escape to close without action. Read the [palette tutorial](help:tutorial.palette) for a safe practice sequence.
