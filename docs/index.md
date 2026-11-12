# GUC Mail CLI documentation

GUC Mail CLI is an independent, unofficial terminal client for a German University in Cairo
Exchange mailbox. This documentation uses synthetic examples. It contains no real mailbox data.

## Choose a starting point

- **New to command-line programs:** follow [Getting started](getting-started/index.md), then keep the
  [nontechnical user guide](user-guide/index.md) nearby.
- **Comfortable with automation:** start with [Advanced use](advanced/index.md) and the
  [command reference](command-reference/index.md).
- **Extending the application:** read [Development](development/index.md), then
  [Architecture](architecture/index.md) and the [API contracts](api/index.md).
- **Something failed:** open [Troubleshooting by symptom](troubleshooting/index.md).
- **A word is unfamiliar:** use the [Glossary](glossary/index.md).

## Everyday areas

- [Mail features](features/index.md)
- [Scheduled messages and Undo Send](scheduling/index.md)
- [Calendar](calendar/index.md)
- [Contacts](contacts/index.md)
- [Tasks](tasks/index.md)
- [Search and local indexing](search/index.md)
- [Backup and offline browsing](backup/index.md)
- [New-mail monitoring](monitoring/index.md)
- [Notifications and webhooks](integrations/index.md)
- [Privacy and security](privacy-and-security/index.md)

## Technical references

- [Complete command-family reference](command-reference/index.md)
- [Configuration and environment-variable reference](configuration/index.md)
- [Advanced workflows and structured output](advanced/index.md)
- [EWS and SOAP capability matrix](ews/index.md)
- [Architecture](architecture/index.md)
- [Developer guide](development/index.md)
- [API and data contracts](api/index.md)

## How to read feature status

Exchange Web Services (EWS) behavior depends on the Exchange version, server configuration, and
account permission. Documentation distinguishes these states:

- **Fixture tested:** implemented and covered by synthetic tests, but not verified against the GUC
  server.
- **Live verified:** confirmed through an explicitly recorded live check.
- **Partial:** only some behavior is available, or a bounded local fallback is used.
- **Unsupported or unauthorized:** the detected server or account did not permit the operation.
- **Not probed:** no live claim is possible yet.

Unless a page explicitly records a live check, treat it as fixture-tested or statically documented.
No documentation command is permission to send, move, delete, or otherwise change real mailbox
data.

## Documentation pattern

Feature pages begin with a quick explanation, then give steps and copyable commands. Advanced
behavior follows after the ordinary workflow. Troubleshooting and privacy notes appear near the
action they affect. Commands that can change data are marked **mutating**; actions that can make
data difficult to recover are marked **destructive**.
