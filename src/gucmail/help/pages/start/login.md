# Sign in and protect your password

GUC Mail CLI needs an email identity, sign-in name, HTTPS EWS endpoint, and password. The normal configuration file stores the non-secret values. The password belongs in the operating-system keyring or a hidden prompt.

## Recommended setup

Run `gucmail login`. Confirm the account identity, enter the password through the hidden prompt, and allow one bounded status check. On Windows, keyring storage normally means Windows Credential Manager. `gucmail logout` removes the selected keyring entry; it does not delete mail, drafts, schedules, or backups.

Environment variables are useful for a controlled process, but shell history and process inspection make command-line passwords unsafe. A legacy `creds.env` in the current working directory is a compatibility fallback. It is parsed as quoted data, never executed. Prefer migrating to the keyring and remove the plaintext file yourself only after verifying login.

## When login fails

Do not retry rapidly. Check keyboard layout, username, system clock, endpoint, and whether the password changed. Run local diagnostics first, then an intentional network or authentication check. See [Connection and login problems](help:troubleshooting.connection) and [Credentials and legacy files](help:privacy.credentials).
