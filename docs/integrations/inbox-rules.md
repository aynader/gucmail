# Exchange inbox rules

Open **Notifications → Exchange inbox rules**, or use **Manage Exchange Inbox Rules**
in the command palette. These are server mailbox policies; local notification rules
control alert delivery separately.

`gucmail inbox-rules list` and `show RULE_ID` read names, priority, enabled/support
state, conditions, exceptions and actions. EWS returns the whole rule collection in
one response; CLI limit/offset slice that collection. The adapter refuses more than
1000 rules, duplicate IDs or a rule over 256 KiB. Inspection reads no message bodies
and changes no read state.

The guided form and `inbox-rules create` support sender/subject substring alternatives,
attachment presence, categories, marking read, moving to a mailbox folder and stopping
later rules. Alternatives within a condition are OR; different conditions are AND.
At least one condition and action are required. Creation starts disabled. A separate
`enable RULE_ID` requires review before the rule can act on incoming mail.

```powershell
gucmail inbox-rules create --name "Course notices" --subject-contains "Lecture" --category "Course" --dry-run
gucmail inbox-rules show RULE_ID
gucmail inbox-rules enable RULE_ID --dry-run
gucmail --yes inbox-rules enable RULE_ID
gucmail inbox-rules disable RULE_ID --dry-run
gucmail inbox-rules delete RULE_ID --dry-run
```

Every write requires confirmation or global `--yes`. Dry-run displays the exact plan.
Human output displays the plan before confirmation; JSON returns a single structured
result. Enable/disable preserves native conditions, exceptions and actions. Unsupported
or errored policies can be inspected and explicitly deleted, but cannot be rewritten.
Arbitrary policy editing, forwarding and permanent-delete rule creation are not exposed.

All mutations send `RemoveOutlookRuleBlob=false`. There is no fallback that clears
Outlook's client-side policy data. Microsoft describes the client-side rule loss risk
in the [UpdateInboxRules documentation](https://learn.microsoft.com/en-us/exchange/client-developer/web-service-reference/updateinboxrules-operation).
Deletion never temporarily enables or rewrites a disabled rule. If Exchange rejects
that deletion, inspect it in Outlook before making another decision.

Fresh rule fingerprints reject changed previews. Create also rechecks the complete
inventory and requires a unique name for readback identification. Exchange can
renumber priorities during insertion; behavior is compared after creation. Accepted
writes with failed/mismatched readback and lost network responses report uncertainty
without replaying the write. Refresh before another attempt. EWS rules have no
ChangeKey, so this is optimistic conflict detection rather than atomic compare-and-set.

Whether your account may read or change inbox rules depends on the server and your
account permissions; a denial is reported as unsupported or unauthorized and nothing
is changed.
