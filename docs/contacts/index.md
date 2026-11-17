# Contacts

Your Outlook contacts, people you've written to, and the GUC directory. Open them with the
**Contacts** tab at the top.

## Find people

```text
gucmail contacts list
gucmail contacts search "Mona Adel"
gucmail contacts show CONTACT_REFERENCE
gucmail directory search "Mona Adel"
gucmail recipients recent
gucmail recipients frequent
```

`contacts` searches your own contacts. `directory` searches the GUC address book, when the GUC
server allows it. For student accounts it often returns nothing.

Long lists come in pages. In the app use Next and Previous. In the terminal add `--offset`.

## Add or change a contact

```text
gucmail contacts create --display-name "Mona Adel" --email mona.adel@example.edu
gucmail contacts edit CONTACT_REFERENCE --phone mobile=0123456789
```

You see every change before it's saved.

## Duplicates

```text
gucmail contacts duplicates
gucmail contacts merge CONTACT_REFERENCE CONTACT_REFERENCE
```

`duplicates` suggests contacts that look the same and says why. `merge` shows the combined contact
and what will be removed before doing anything.

## Import and export

```text
gucmail contacts import contacts.vcf
gucmail contacts export CONTACT_REFERENCE --output contact.vcf
```

Import shows what it will add first. Export won't replace a file unless you add `--overwrite`.

## Groups

**Contacts → Contact groups** lets you make your own groups and write to them. See the
[contact-group guide](groups.md).

## Address suggestions

When you type in To, Cc or Bcc, gucmail suggests addresses you've sent to recently. Press **Right**
to accept one. Suggestions come only from your own sent mail on this computer.

Settings that control this:

| Setting | Does |
|---|---|
| Recipient autocomplete | Suggest addresses while you type |
| Use and remember recent recipients | Keep the list of people you've written to |
| Remember Bcc recipients | Include Bcc addresses. Off by default |
| Recently contacted people | Clear the list |

## Who you write to most

`gucmail recipients frequent` and **Contacts → Local history** look at the headers of your recent
Inbox and Sent mail. They never read message text.

**Index observed contact history**, off by default, keeps those findings on your computer for 30
days so the next look is faster. `gucmail contacts history-index --clear` deletes them.

## Good to know

- Directory search denied? Your own contacts and recent recipients still work.
- "Contact changed": someone edited it elsewhere. Refresh and try again.
- Contacts in a backup can be read offline, but not changed.

## Privacy

Exported `.vcf` files contain addresses, phone numbers and notes. Your address history never
leaves your computer.
