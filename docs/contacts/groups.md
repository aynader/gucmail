# Contact groups

Open **Contacts → Contact groups**, or **Manage Contact Groups** in the command
palette. The workspace starts with account-local groups, stored on this computer.
They survive cache clearing and work offline. Choose **Mailbox · personal groups**
to explicitly load groups from the account's Contacts folder.

Both kinds support creation, naming, membership replacement and exact-reference
deletion. Duplicate names remain separate identities. The guided editor accepts
one email or `Name <email>` per line, with at most 200 recipients. It rejects
invalid or duplicate addresses. Review every recipient before confirmation.

```powershell
gucmail contact-groups create --name "Project team" --member "first@example.edu" --member "second@example.edu" --dry-run
gucmail --yes contact-groups create --name "Project team" --member "first@example.edu"
gucmail contact-groups list
gucmail contact-groups show GROUP_REFERENCE
gucmail contact-groups edit GROUP_REFERENCE --member "replacement@example.edu" --dry-run
gucmail --json contact-groups recipients GROUP_REFERENCE
gucmail contact-groups delete GROUP_REFERENCE --dry-run
gucmail contact-groups list --source mailbox --limit 25 --offset 0
```

Global output flags precede the command. `edit --member` replaces the complete
membership; omitted fields are preserved. Mutations require confirmation or global
`--yes`. `--dry-run` shows a plan without applying it. `recipients` emits explicit
SMTP recipients for review or scripting and sends no mail.

**Compose (Bcc)** fetches fresh recipients, presents their exact list for review,
and opens a Bcc draft. The composer still requires its normal send review and
confirmation. No name is silently expanded at send time by this workflow.

Mailbox groups use installed exchangelib `DistributionList`/`Member` models and
normal item operations. Listing projects metadata; members are fetched on demand.
Edits recheck the revision/content and use `NeverOverwrite`. Local groups use a
transaction and revision/content comparison. Changed previews fail without
overwriting newer membership. Mailbox deletion moves the item to Deleted Items;
local deletion removes the locally saved membership.

Groups with unresolved, nested, non-SMTP or linked members can be inspected.
Membership editing is blocked rather than dropping unsupported members. Groups
with unresolved members cannot be used for composition. This implementation does
not manage institutional/Active Directory distribution or security groups, or
recursively expand directory lists. Microsoft distinguishes personal mailbox
groups from directory groups in its [distribution groups guidance](https://learn.microsoft.com/en-us/exchange/client-developer/exchange-web-services/distribution-groups-and-ews-in-exchange).

Lost mutation responses and accepted changes with failed readback report uncertainty
without replaying the write. Refresh before another change. Whether your mailbox
allows personal contact groups and directory-group expansion depends on the server and
your account permissions; a denial is reported as unsupported or unauthorized.
