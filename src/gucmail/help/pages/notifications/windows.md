# Windows desktop notifications

## The easy way

Press **Ctrl+K** and choose **Turn on notifications**, or flip **Notify me about new mail** at
the top of notification settings. From the command line: `gucmail notifications on`.

This registers gucmail with Windows, starts the background task and sends a test
notification. Each step shows ✓ or ✗ with a short reason. Turning it on again repairs
anything that broke. `gucmail notifications off` turns notifications off; scheduled mail
still gets sent.

The rest of this page covers the details behind that switch.

A desktop notification is produced locally after the watcher detects an event. Availability depends on the installed Windows provider; the application should report unavailable rather than pretending a toast was delivered.

## Privacy choices

Choose how much lock-screen content is acceptable: generic “New mail,” sender only, sender and subject, or a short sanitized preview. Bodies, attachment contents, raw IDs, and credential material do not belong in notifications. Quiet hours and digest behavior should apply before provider delivery.

Use the synthetic notification test before starting mailbox monitoring. The test must not read a mailbox or call an external webhook.

## Desktop setup and preferences

Install the optional desktop extra in the environment that runs GUC Mail:

```text
python -m pip install "gucmail[desktop]"
gucmail --json notifications desktop status
gucmail --json notifications desktop setup
gucmail notifications desktop setup --apply
```

In the TUI, open **Notifications → Destinations → Desktop setup…**.
Setup previews the exact current-user application identity and opening command before
confirmation. Unknown or conflicting registry data blocks changes. Escape cancels.
The same shared service handles CLI and TUI changes and rechecks the preview before applying it.
Setup needs no administrator rights or mailbox credentials.

**Add provider** or **Edit policy** exposes the generic title, short/long duration,
sound, grouping and attachment/importance indicators for Windows desktop delivery.
Save applies these non-secret options; Cancel discards edits. Privacy still removes
restricted sender, subject and preview fields before delivery. Selecting silent mode
skips a fallback that cannot guarantee silence, keeping the event in Activity history.
Private mode also withholds the local reference and disables notification opening.
Other modes open only a known message mapping in the configured account's local state.

The Windows-Toasts backend constructs native requests using this application's own
identity. It supports account-bound opening into a small read-only reader, sound and
duration requests, grouping and stable event tags. The reader resolves only the exact
local message reference and account, prompts securely if necessary, retrieves one
message and preserves its read state. It does not fetch remote HTML resources.

**Verification limit:** synthetic tests and actual SDK/WinRT XML serialization passed;
visible Windows display, sound, lock-screen behavior and opening after the watcher
exits have not been verified. API acceptance is reported separately from visible delivery.
Read/reply action buttons are unavailable. Without owned setup or a usable SDK, the
Plyer fallback has no dependable opening, native grouping or sound control. Use Activity
to inspect the local message reference.

Preview removal with `gucmail notifications desktop remove`; use `--apply` and
confirmation to remove only the exact owned registration. Before changing Python or
application paths, remove the old registration, then set up the new one. A partial
failure preserves the manifest and reports uncertain changes; inspect before retrying.
Unowned extra registry values, child keys or a changed manifest are preserved.

See the [desktop setup guide](https://windows-toasts.readthedocs.io/en/stable/custom_aumid.html)
for the optional SDK's own-identity mechanism. This application does not borrow another
program's notification identity.

Notification history records provider, state, time, attempt count, and sanitized detail. A displayed toast is not evidence that continuous monitoring is running. To continue after the TUI closes, configure a supported background watcher and review [Start monitoring with Windows](help:startup.windows).
