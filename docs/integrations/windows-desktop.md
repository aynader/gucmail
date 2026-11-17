# Windows desktop notifications

## The easy way

```text
gucmail notifications on
```

This registers gucmail with Windows, starts the background task and sends a test notification.
Each step shows ✓ or ✗. `gucmail notifications off` turns them off. The rest of this page is for
setting up each piece by hand.

Desktop notifications are optional local output from the watcher. They do not enable
Exchange push subscriptions or start monitoring by themselves.

## Setup by hand

Install into the same Python environment as GUC Mail:

```text
python -m pip install "gucmail[desktop]"
gucmail --json notifications desktop status
gucmail --json notifications desktop setup
gucmail notifications desktop setup --apply
```

The desktop extra includes Plyer and, on Windows, Windows-Toasts 1.3.1. Native
requests require Windows 10 build 15063 or later, a usable optional SDK and the
application's own current-user registration. Setup is opt-in. No registration
occurs during import, provider discovery or a preview.

In the TUI, open the command palette with Ctrl+K, choose **Open Notifications**, then
**Providers & Privacy → Desktop setup…**. Review the exact application ID, protocol
scheme, manifest and opening command. **Setup** asks for confirmation; Escape cancels.
The CLI requires `--apply` and confirmation, or explicit `--yes` for automation.
All setup operations are local and need neither administrator access nor mailbox credentials.

Only this application's exact HKCU application-identity and protocol keys are eligible
for modification. Unknown state, denial, unowned markers, extra values or children,
changed manifests and changes after preview block application. Partial failures are
reported as uncertain and preserve the local manifest for inspection. Removal uses the
same checks; it never deletes an arbitrary registry tree.

## Preferences and delivery

Use **Add provider** or **Edit policy** to choose the generic title, Short/Long duration,
sound, folder grouping and attachment/importance indicators. These options are stored
with that provider's exact ID and consumed by the shared desktop adapter used by the
watcher, headless runner, CLI and Activity test. Save persists them; Cancel preserves
the previous settings. Invalid saved values open a reviewable editor with an explanation.

Profile privacy, quiet hours, additional provider filters and burst limits still apply
before delivery. The generic title is used when a sender title is unavailable; sender
and subject modes may select the permitted sender title instead. Indicators reflect
only fields that the privacy policy allowed through.

Private mode withholds the local reference and therefore disables opening from the
notification. Modes that permit a reference but hide the account can open only a
known message mapping in the configured account's local state. Missing, draft,
cross-account or unavailable mappings keep text delivery and omit activation.

```text
gucmail --json notifications providers test desktop
gucmail notifications providers test desktop --deliver
gucmail watch --folder inbox --desktop
gucmail notifications history
```

The test is a synthetic preview by default. `--deliver` asks for confirmation and may
display one local notification; it does not read the mailbox. Monitoring deliberately
uses the configured mailbox. Follow [monitoring](../monitoring/index.md) before starting it.

If native setup or the SDK is unavailable, the optional Plyer fallback provides basic
display without dependable opening, action buttons, native grouping or sound control.
Silent settings skip that fallback rather than risk sound; Activity history retains
the outcome. The portable fallback also honors the silent choice by skipping delivery;
Windows duration, grouping and indicator controls are not portable desktop guarantees.

## Opening and removal

Native opening targets a dedicated read-only entry point, not the general command parser.
The canonical token contains an exact local message reference and a hash binding the
account and endpoint. It contains no password, message body or raw EWS ID. Tampered,
legacy unbound, cross-account, unknown, draft and option-shaped targets are rejected.
The current owned registration, configured account and local mapping must match before
credentials are requested. The reader retrieves the selected message on demand without
marking it read. HTML becomes sanitized text; remote images and active content are not loaded.

To remove the owned registration:

```text
gucmail --json notifications desktop remove
gucmail notifications desktop remove --apply
```

After changing Python or application paths, remove the old registration and set up the
new one. Removal can recognize the old owned manifest even if its former Python executable
has gone. If a setup fails part way through, inspect it before retrying. A malformed
manifest or unowned registry change requires manual review; the application preserves it.

## Verification boundary

Automated tests use owned synthetic registry, notifier and mailbox doubles. A separate
private environment tested the actual Windows-Toasts 1.3.1 data types and WinRT XML DOM
serialization, with notifier creation and display trapped. That proves the request
contract, not visible notification delivery or native protocol execution.

Actual product registration, Windows display, sound, grouping, lock-screen behavior,
Action Center retention and opening after the watcher exits remain unverified. Native
read/reply action buttons are not implemented. A successful API call reports acceptance,
not proof that Windows showed a toast.

The optional SDK's [custom application identity guide](https://windows-toasts.readthedocs.io/en/stable/custom_aumid.html)
and [interactive toast documentation](https://windows-toasts.readthedocs.io/en/stable/interactable.html)
describe its mechanism. Microsoft documents the native
[toast tag size constraint](https://learn.microsoft.com/en-us/uwp/api/windows.ui.notifications.toastnotification.tag?view=winrt-26100)
that sets this adapter's minimum Windows build.
