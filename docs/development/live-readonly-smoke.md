# Bounded live read-only smoke test

Use this opt-in script when you want to validate a real GUC mailbox without changing it. It runs
five logical tests through one reused Exchange session: account status, folders, Inbox message
summaries, a 14-day calendar metadata window, and metadata-only contacts/tasks pages.

The request guard permits only `ConvertId`, `FindFolder`, `FindItem`, `GetFolder`, and `GetItem`.
Every other SOAP operation is blocked before transport. Returned item limits are three to five,
requests have a hard budget, redirects or unexpected destinations are blocked, and the script never
prints credentials, subjects, addresses, identifiers, bodies, attachments, response content, or
private error text. It creates no normal app logs, directories, or SQLite references.

From Windows PowerShell in the repository root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe qa\live_readonly_smoke.py --credentials .\creds.env
```

The legacy file is parsed as data, never executed. Its accepted format is limited to quoted scalar
assignments:

```text
email = "your-address"
username = "your-login-name"
password = "your-password"
```

A successful run ends with `tests=5/5`, reports logical SOAP and physical HTTP counts, and confirms
`mutating SOAP operations allowed=0`. HTTP 401 entries can be expected NTLM challenge exchanges;
the final authenticated operations should return HTTP 200. Any failed stage stops subsequent checks.

To verify native change synchronization instead of repeating those five checks:

```powershell
.\.venv\Scripts\python.exe qa\live_readonly_smoke.py --native-sync-only
```

This separate mode permits only `ConvertId`, `GetFolder` and `SyncFolderItems`. It requests one
Inbox page and one Calendar page, with `IdOnly`, selected metadata and exactly three maximum
changes per page. The guard rejects larger sync pages and body/MIME/attachment property requests.
It does not drain continuation pages, deliver notifications or save sync state to the normal
application database. A successful run reports `tests=2/2`. Provider conversion warnings are
suppressed because their text can contain server field values; failed operations still stop the run.

After validation, prefer `gucmail login` and the operating-system keyring. Remove the plaintext
legacy file yourself only after verifying the stored login works.

