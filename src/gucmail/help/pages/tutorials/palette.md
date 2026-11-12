# Tutorial: use the command palette

Practice with read-only requests so you can see matching, extracted fields, and confidence without changing the mailbox.

## Try these requests

1. Open the palette and type “open help about backups.” Confirm that a Help article ranks near the top.
2. Type a slightly misspelled request for unread mail from Ahmed Example. Review whether the proposed Search Mail command, sender slot, and unread filter are correct.
3. Type “show calendar this week.” If calendar capability is unknown or unavailable, inspect the explanation rather than retrying.
4. Type an incomplete request such as “move these.” The palette should require a destination and selected-message context.
5. Press Escape. No action should run.

Search, Help, settings, and navigation actions can execute directly when harmless. Compose or mailbox changes must show a preview. Destructive requests require exact wording, an exact scope, and confirmation. The interpreter is deterministic and local; it does not contact an AI service. See [Search mail](help:search.overview) for query syntax.
