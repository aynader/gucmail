# Credentials, keyring, and legacy files

Credential priority is deliberate: intentionally supplied values, process environment, operating-system keyring, secure interactive prompt, then the legacy file compatibility path when allowed. Status output reports the source without printing the secret.

## Recommended storage

Use `gucmail login` and the OS keyring. The configuration file stores account identity and endpoint but no password. `gucmail logout` removes the selected keyring credential; it does not erase local drafts, schedules, indexes, or backups.

A legacy `creds.env` contains quoted email, username, and password assignments in plaintext. The parser accepts only those scalar fields and never executes the file. When the application is launched from a directory containing that file and stronger sources do not provide a password, it can be used as the final compatibility source. Move to the keyring and remove the plaintext file manually after validation.

Never include credentials in commands, screenshots, logs, diagnostics, tests, or Help. Keep TLS certificate verification enabled. The endpoint must be credential-free HTTPS with no query or fragment.
