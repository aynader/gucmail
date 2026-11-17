# Getting started

From a clean computer to reading your mail in about ten minutes. gucmail is independent and
**not an official GUC or Microsoft product**.

You need Windows 10 or 11 (Linux and macOS work too), Python 3.11 or newer, and your GUC email
and password.

## Install

Open **PowerShell** from the Start menu. Type the commands after the prompt, not the prompt.

```powershell
py -3.11 -m pip install --user pipx
py -3.11 -m pipx ensurepath
```

Close PowerShell, open it again, then install gucmail.

```text
pipx install "gucmail[desktop]"
```

`[desktop]` adds Windows notifications. Leave it out if you don't want them.

On Linux or macOS use `python3` instead of `py -3.11`.

## Sign in

```text
gucmail
```

The first time, gucmail shows one sign-in screen.

1. Type your GUC email and password.
2. Press **Test & save**. gucmail checks them with one small request.
3. If they work, your password goes into the system password store (Windows Credential
   Manager) and your inbox opens. If not, you get a plain reason and nothing is saved.

A three-step tour follows. Press Esc to skip it.

If your email doesn't work as the sign-in name, open **More options** and type
`firstname.lastname`. **Not now** closes the screen. Press F5 to sign in later.

`gucmail login` does the same from the terminal, with a hidden password prompt.

## Find your way around

| Key | Does |
|---|---|
| ↑ ↓ | Move through messages |
| Tab | Move between folders, list and reader |
| Enter | Open a message |
| Esc | Go back |
| c | Write a new message |
| r, a, f | Reply, reply all, forward |
| d, e, ! | Delete, archive, flag. Ctrl+Z undoes |
| / | Search |
| Ctrl+K | Do anything by typing what you want |
| ? | Help |
| q | Quit |

The mouse works too. The bottom line shows the keys for the screen you're on.

Remote images stay blocked, and opening a message doesn't mark it read unless you turn that on.

## Send a message

1. Press `c`.
2. Fill in **To**, **Subject** and the message.
3. Choose **Review send** and check the recipients.
4. Confirm.

If the connection drops at the wrong moment, gucmail marks the message **uncertain** instead of
sending it twice. Check **Sent** before trying again.

## Practice without your mailbox

```text
gucmail tui --offline
```

Press `?` and choose **Tutorials**. Seven short lessons use sample mail and never send anything.

## Update

If new-mail alerts run in the background, stop them first. On Windows a running alert process
blocks the update.

```text
gucmail headless stop
pipx upgrade gucmail
```

Updating keeps your settings, drafts and scheduled mail.

## Sign out or uninstall

```text
gucmail logout
pipx uninstall gucmail
```

`logout` removes the saved password only. Your drafts, backups and settings stay until you delete
them. See [where gucmail keeps data](../privacy-and-security/local-data-locations-and-retention.md).

## If something goes wrong

| Problem | Try |
|---|---|
| `gucmail` is not recognized | Open a new PowerShell. If it still fails, run `py -3.11 -m pipx ensurepath` again |
| Password rejected | Press F5 in the app, or run `gucmail login` |
| Can't reach the server | Check your connection, then press F5 once |
| The screen feels cramped | Make the window wider, 120 columns shows everything |
| "Not allowed" or "unsupported" | Nothing was changed. The GUC server doesn't offer that feature |

`gucmail doctor` checks your setup without touching your mail. More help is in
[Troubleshooting](../troubleshooting/index.md).

## Next

- [User guide](../user-guide/index.md)
- [Scheduling mail](../scheduling/index.md)
- [Search](../search/index.md)
- [Backups](../backup/index.md)
- [Privacy and security](../privacy-and-security/index.md)
- [All commands](../command-reference/index.md)
