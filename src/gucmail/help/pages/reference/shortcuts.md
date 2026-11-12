# Keyboard shortcuts

Keyboard bindings are context-sensitive. The active footer and shortcut browser are the authoritative source because a key can be unavailable or have a different scope on another screen.

Open **Help → Keys** or **Command Palette → Keyboard Shortcuts**. Filter by area or
search a key, action or screen. The browser reads current screen and focused-control
bindings, including hidden bindings. Select a customizable row and press Ctrl+Enter
or choose Customize. Application actions open the guided editor at that action;
the four palette/theme keys open their exact Settings entries. **Edit keys** opens
the complete editor. Settings also provides **Keyboard → Action assignments → Edit keys**.

Application action keys, the two palette keys and previous/next theme keys are customizable. Blank disables
an assignment. Use Ctrl/Alt plus a supported key or F1-F24. Stage checks the key's
syntax; Apply validates all action assignments and the four global keys together.
Stage both sides of a swap before Apply. Cancel preserves saved and running bindings. Different actions
cannot share a global key. Two aliases for the same palette action can share one key.
Editing/navigation keys are protected. Existing contextual reuse (such as Ctrl+P
for theme preview) is shown separately from conflicting assignments. Navigation,
clipboard, confirmation and system controls remain fixed. A mailbox action remap
does not acquire a new meaning in an unrelated workspace or focused composer.

In the editor, Ctrl+Enter stages, Ctrl+S applies and Ctrl+Z cancels. Reset key stages
one default. Reset all asks for confirmation and stages unlocked defaults; Apply
remains separate. Environment/policy overrides stay locked. Settings history and
Undo include saved keyboard changes.

Export saves the action map and four global keys only; replacing a file requires
confirmation. Preview reads a complete keyboard file into Proposed without saving;
Import confirms and applies that preview. Cancel discards it. Files over 4 MiB,
unknown actions, missing/extra settings and conflicting keys are rejected.
Configuration changes elsewhere invalidate the preview: refresh and review again.

Help refreshes current assignments after returning from Settings. Invalid legacy
or environment combinations activate safe defaults and show a repair notice; the
application does not rewrite saved preferences automatically. An invalid saved
action map can be repaired in Edit keys with Reset all then Apply. Remove an invalid
environment override before editing.
The CLI's offline article provides a curated reference; the mounted TUI browser
includes the actual runtime screen/control binding metadata.

## Common navigation

Arrow keys move within the focused control. Page Up, Page Down, Home, and End move by larger steps when supported. Tab and Shift+Tab change focus. Enter opens or activates, while Escape or Backspace returns without committing unfinished work.

Common mail actions include Compose, Reply, Reply all, Forward, Search, Refresh, Help, and the command palette. A displayed shortcut should come from the same binding registry used by the TUI, not a copied documentation table. User-customized bindings and conflicts need validation before saving.

Mouse interaction is optional. Any mouse-only divider or button must have a keyboard equivalent. In confirmation dialogs, focus and labels must clearly distinguish Cancel from the action. Monochrome and small-terminal modes retain visible focus indicators. Open [Interface tour](help:start.interface) for pane behavior and [Layout](help:appearance.layout) for resize controls.
