# Doctor, diagnostics, and safe support information

Doctor begins with local, non-authenticated checks: application and package versions, configuration validity, writable directories, keyring availability, cache/state health, and notification configuration.

## Optional checks

Network mode adds bounded DNS, TCP, TLS, and endpoint response checks. Authentication is a separate explicit choice and should make one bounded mailbox-status read through a reused session. Doctor does not send, move, delete, mark, or download mailbox content.

Diagnostics can write a sanitized bundle containing versions, capability state, path health, correlation identifiers, and redacted errors. It must exclude passwords, environment secrets, keyring values, cookies, authorization headers, bodies, attachments, raw addresses, and unreviewed SOAP.

Review the output before sharing. A debug traceback may still reveal local paths or operational metadata, so debug mode is not automatically safe to publish. Error-aware Help should link from a translated error to the relevant symptom page. Use [Connection troubleshooting](help:troubleshooting.connection) or [Database errors](help:troubleshooting.database) based on the reported class.
