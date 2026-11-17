# Tasks

Your Outlook tasks. Open them with the **Tasks** tab at the top.

## Everyday use

```text
gucmail tasks list
gucmail tasks search "report"
gucmail tasks show TASK_REFERENCE
gucmail tasks create --subject "Submit report" --due 2026-08-26
gucmail tasks edit TASK_REFERENCE --due 2026-08-27
gucmail tasks complete TASK_REFERENCE
gucmail tasks reopen TASK_REFERENCE
```

A task can be not started, in progress, waiting on others, deferred or completed. Completing it
sets it to 100%.

In the app, **Ctrl+K → Create task from message** turns the selected email into a task. Nothing is
saved until you press Save, and the email isn't moved or marked read.

## Reminders

```text
gucmail tasks create --subject "Call advisor" --reminder-at 2026-08-24T09:00:00+03:00
gucmail tasks edit TASK_REFERENCE --clear-reminder
```

## Repeating tasks

A task can repeat on fixed dates or a set time after you complete it.

```text
gucmail tasks create --subject "Weekly review" --repeat weekly
gucmail tasks create --subject "Water plants" --repeat after_completion_daily --repeat-interval 5
```

End a repeat with `--repeat-count`, `--repeat-until 2026-12-31` or `--repeat-forever`. The GUC
server creates the next task, not gucmail.

**Settings → Tasks** sets the repeat new tasks start with. It's None by default.

Add `--dry-run` to any of these to see what would be created without creating it.

## Delete

```text
gucmail tasks delete TASK_REFERENCE
```

This moves the task to Deleted Items, where you can get it back. Deleting a repeating task this way
removes the whole series. `--permanent` deletes for good and asks you to type a confirmation.

## History and export

```text
gucmail tasks history TASK_REFERENCE
gucmail tasks export --output tasks.json --format json
```

`history` lists the changes you made with gucmail on this computer. It can't show changes made
elsewhere.

## Good to know

- Long lists come in pages. In the app use Next and Previous. In the terminal add `--offset`.
- "Task changed": it was edited elsewhere. Refresh and try again.
- Tasks in a backup can be read offline, but not changed.
- Changing only one occurrence of a repeating task isn't supported yet.
