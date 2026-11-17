"""Capability-honest catalog of user, local, and privileged EWS operations."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from gucmail.models import (
    AccountAccess,
    Capability,
    CapabilityStatus,
    ImplementationPath,
    ImplementationStatus,
    SafetyClass,
    ServerSupport,
    VerificationLevel,
)


def _cap(
    operation: str,
    family: str,
    safety: SafetyClass,
    *,
    path: ImplementationPath = ImplementationPath.EXCHANGELIB,
    implemented: ImplementationStatus = ImplementationStatus.IMPLEMENTED,
    minimum_version: str | None = "Exchange2007_SP1",
    retry_class: str | None = None,
    dry_run: bool | None = None,
    fallback: str | None = None,
    reason: str | None = None,
    access: AccountAccess = AccountAccess.NOT_PROBED,
    verification: VerificationLevel = VerificationLevel.STATIC,
) -> Capability:
    if access is AccountAccess.ADMIN_ONLY:
        status = CapabilityStatus.ADMIN_ONLY
    elif implemented is ImplementationStatus.NOT_IMPLEMENTED:
        status = CapabilityStatus.PARTIAL if fallback else CapabilityStatus.NOT_IMPLEMENTED
    elif implemented is ImplementationStatus.PARTIAL:
        status = CapabilityStatus.PARTIAL
    else:
        status = CapabilityStatus.NOT_LIVE_VERIFIED
    if retry_class is None:
        retry_class = "read_only" if safety is SafetyClass.READ_ONLY else "never_after_ambiguous"
    return Capability(
        operation=operation,
        family=family,
        safety=safety,
        status=status,
        minimum_version=minimum_version,
        implementation_path=path,
        implementation_status=implemented,
        server_support=ServerSupport.NOT_PROBED,
        account_access=access,
        verification=verification,
        retry_class=retry_class,
        dry_run=(safety is not SafetyClass.READ_ONLY) if dry_run is None else dry_run,
        fallback=fallback,
        reason=reason,
    )


def _catalog_only(
    operation: str,
    family: str,
    safety: SafetyClass,
    *,
    minimum_version: str | None = "Exchange2007_SP1",
    fallback: str | None = None,
    reason: str | None = None,
) -> Capability:
    """Represent an EWS operation without claiming that an adapter exists for it."""

    return _cap(
        operation,
        family,
        safety,
        path=ImplementationPath.REGISTRY_ONLY,
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        minimum_version=minimum_version,
        fallback=fallback,
        reason=reason or "The safe client currently has no typed adapter for this operation.",
    )


_OPERATIONS: tuple[Capability, ...] = (
    # Folder operations
    _cap("GetFolder", "folders", SafetyClass.READ_ONLY),
    _cap("FindFolder", "folders", SafetyClass.READ_ONLY),
    _cap("CreateFolder", "folders", SafetyClass.MUTATING),
    _cap(
        "CreateFolderPath",
        "folders",
        SafetyClass.MUTATING,
        path=ImplementationPath.SOAP_TYPED,
        implemented=ImplementationStatus.PARTIAL,
        fallback="Create and verify one folder segment at a time.",
        reason="A dedicated typed CreateFolderPath serializer is not implemented.",
    ),
    _cap("UpdateFolder", "folders", SafetyClass.MUTATING),
    _cap(
        "MoveFolder", "folders", SafetyClass.MUTATING, verification=VerificationLevel.FIXTURE_TESTED
    ),
    _cap(
        "CopyFolder",
        "folders",
        SafetyClass.MUTATING,
        path=ImplementationPath.SOAP_TYPED,
        verification=VerificationLevel.FIXTURE_TESTED,
    ),
    _cap("DeleteFolder", "folders", SafetyClass.DESTRUCTIVE),
    _cap("EmptyFolder", "folders", SafetyClass.DESTRUCTIVE),
    # Generic items and mail
    _cap("GetItem", "items", SafetyClass.READ_ONLY),
    _cap("FindItem", "items", SafetyClass.READ_ONLY),
    _cap("CreateItem", "items", SafetyClass.MUTATING),
    _cap("UpdateItem", "items", SafetyClass.MUTATING),
    _cap("MoveItem", "items", SafetyClass.MUTATING),
    _cap("CopyItem", "items", SafetyClass.MUTATING),
    _cap("DeleteItem", "items", SafetyClass.DESTRUCTIVE),
    _cap("SendItem", "messages", SafetyClass.MUTATING, retry_class="ambiguous_reconcile"),
    _catalog_only(
        "ArchiveItem",
        "items",
        SafetyClass.MUTATING,
        minimum_version="Exchange2013",
        fallback="Move the item to an explicitly selected Archive folder when one is available.",
    ),
    _cap(
        "MarkAllItemsAsRead",
        "items",
        SafetyClass.MUTATING,
        path=ImplementationPath.SOAP_TYPED,
        verification=VerificationLevel.FIXTURE_TESTED,
    ),
    _catalog_only(
        "MarkAsJunk",
        "messages",
        SafetyClass.MUTATING,
        minimum_version="Exchange2013",
        fallback="Move the message to the Junk folder without changing server junk classification.",
    ),
    _cap(
        "ExportItems",
        "items",
        SafetyClass.READ_ONLY,
        implemented=ImplementationStatus.PARTIAL,
        verification=VerificationLevel.FIXTURE_TESTED,
        minimum_version="Exchange2010_SP1",
        fallback="Native streams are limited to embedded PIM backups; other selected exports "
        "use bounded JSON/MIME. Server and account permission remain unprobed.",
        reason="One containing PIM item with embedded attachments can be archived as a "
        "validated bounded opaque stream; general export and live permissions remain unverified.",
    ),
    _cap(
        "UploadItems",
        "items",
        SafetyClass.MUTATING,
        implemented=ImplementationStatus.PARTIAL,
        verification=VerificationLevel.FIXTURE_TESTED,
        minimum_version="Exchange2010_SP1",
        reason="Explicitly confirmed backup restore creates one ordinary PIM item in a "
        "dedicated folder and verifies its new type/identity. No Update, associated-item "
        "or arbitrary-stream import command; live compatibility remains unverified.",
    ),
    # Attachments
    _cap("GetAttachment", "attachments", SafetyClass.READ_ONLY),
    _cap("CreateAttachment", "attachments", SafetyClass.MUTATING),
    _cap(
        "DeleteAttachment",
        "attachments",
        SafetyClass.MUTATING,
        reason="Available for explicitly selected calendar attachments.",
        verification=VerificationLevel.FIXTURE_TESTED,
    ),
    _cap(
        "GetUserPhoto",
        "people",
        SafetyClass.READ_ONLY,
        path=ImplementationPath.SOAP_TYPED,
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        reason="No typed user-photo request adapter is currently exposed.",
    ),
    # Calendar and meetings
    _cap("GetUserAvailability", "calendar", SafetyClass.READ_ONLY),
    _cap(
        "GetServerTimeZones",
        "calendar",
        SafetyClass.READ_ONLY,
        minimum_version="Exchange2010",
        implemented=ImplementationStatus.PARTIAL,
        reason="Internal exchangelib dependency for availability; no standalone timezone command.",
        fallback="Use the configured local IANA timezone for calendar rendering.",
    ),
    _cap(
        "GetReminders",
        "calendar",
        SafetyClass.READ_ONLY,
        path=ImplementationPath.SOAP_TYPED,
        minimum_version="Exchange2013",
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        reason="Reminder retrieval is represented but has no typed adapter yet.",
    ),
    _cap(
        "PerformReminderAction",
        "calendar",
        SafetyClass.MUTATING,
        path=ImplementationPath.SOAP_TYPED,
        minimum_version="Exchange2013",
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        reason="Reminder actions are represented but have no typed adapter yet.",
    ),
    _catalog_only(
        "GetRoomLists", "calendar", SafetyClass.READ_ONLY, minimum_version="Exchange2010"
    ),
    _catalog_only("GetRooms", "calendar", SafetyClass.READ_ONLY, minimum_version="Exchange2010"),
    _cap("CreateMeeting", "calendar", SafetyClass.MUTATING),
    _cap("UpdateMeeting", "calendar", SafetyClass.MUTATING),
    _cap("CancelMeeting", "calendar", SafetyClass.MUTATING),
    _cap("RespondToMeeting", "calendar", SafetyClass.MUTATING),
    _cap(
        "CalendarRecurrence",
        "calendar",
        SafetyClass.MUTATING,
        verification=VerificationLevel.FIXTURE_TESTED,
        reason=(
            "Typed recurrence read/write mapping is implemented for supported patterns; "
            "update and delete resolve occurrence versus series scope explicitly. "
            "Explicit bounded current occurrence metadata is available read-only. "
            "This entry does not claim this-and-future series splitting, a complete "
            "historical audit, or lossless exception-history restore."
        ),
    ),
    # Contacts, people and directory
    _cap("CreateContact", "contacts", SafetyClass.MUTATING),
    _cap("UpdateContact", "contacts", SafetyClass.MUTATING),
    _cap("DeleteContact", "contacts", SafetyClass.DESTRUCTIVE),
    _catalog_only(
        "FindPeople",
        "contacts",
        SafetyClass.READ_ONLY,
        minimum_version="Exchange2013",
        fallback="Use bounded Contacts-folder search and ResolveNames.",
    ),
    _catalog_only(
        "GetPersona",
        "contacts",
        SafetyClass.READ_ONLY,
        minimum_version="Exchange2013",
        fallback="Use bounded Contacts-folder detail retrieval.",
    ),
    _cap("ResolveNames", "directory", SafetyClass.READ_ONLY),
    _catalog_only("ExpandDL", "directory", SafetyClass.READ_ONLY),
    # Tasks, posts and notes
    _cap("CreateTask", "tasks", SafetyClass.MUTATING),
    _cap("UpdateTask", "tasks", SafetyClass.MUTATING),
    _cap("DeleteTask", "tasks", SafetyClass.DESTRUCTIVE),
    _cap(
        "TaskRecurrence",
        "tasks",
        SafetyClass.MUTATING,
        implemented=ImplementationStatus.PARTIAL,
        verification=VerificationLevel.FIXTURE_TESTED,
        fallback="Use base-series recurrence writes; occurrence-scoped changes remain unavailable.",
        reason=(
            "The supported typed pattern and boundary subset is serialized for task create, "
            "update, read, series deletion, and simple backup restore, including task-only "
            "completion-relative regeneration. Task UpdateItem identity splits keep the "
            "active series and returned one-off instance distinct. Explicit permanent "
            "occurrence/series deletion has typed intent and high-friction review; each active "
            "series is verified with a bounded metadata-only read before deletion. "
            "A bounded local journal links observed accepted updates and returned instances, "
            "with explicit readback outcomes; it does not reconstruct unseen server history. "
            "Occurrence-scoped edits "
            "and complete recurrence-history restore remain unavailable. Synthetic live evidence "
            "covers selected task cases, not every supported pattern or account."
        ),
    ),
    _catalog_only("CreatePostItem", "items", SafetyClass.MUTATING),
    _cap(
        "StickyNote",
        "notes",
        SafetyClass.MUTATING,
        path=ImplementationPath.REGISTRY_ONLY,
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        reason=(
            "exchangelib has no dedicated sticky-note model; normal mail features are unaffected."
        ),
    ),
    # Conversations
    _cap(
        "FindConversation",
        "conversations",
        SafetyClass.READ_ONLY,
        path=ImplementationPath.SOAP_TYPED,
        minimum_version="Exchange2010",
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        reason="Conversation discovery has no typed adapter yet.",
    ),
    _cap(
        "GetConversationItems",
        "conversations",
        SafetyClass.READ_ONLY,
        path=ImplementationPath.SOAP_TYPED,
        minimum_version="Exchange2010_SP1",
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        reason="Conversation-item retrieval has no typed adapter yet.",
    ),
    _cap(
        "ApplyConversationAction",
        "conversations",
        SafetyClass.MUTATING,
        path=ImplementationPath.SOAP_TYPED,
        minimum_version="Exchange2010",
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        reason="Conversation actions have no typed adapter yet.",
    ),
    # Rules and automatic replies
    _cap(
        "GetInboxRules",
        "rules",
        SafetyClass.READ_ONLY,
        minimum_version="Exchange2010_SP1",
        verification=VerificationLevel.FIXTURE_TESTED,
        reason="Typed rule-policy read, capped at 1000 rules; server/account access unprobed.",
    ),
    _cap(
        "UpdateInboxRules",
        "rules",
        SafetyClass.MUTATING,
        minimum_version="Exchange2010_SP1",
        implemented=ImplementationStatus.PARTIAL,
        verification=VerificationLevel.FIXTURE_TESTED,
        reason=(
            "Disabled simple-rule creation, enable/disable and exact-ID deletion preserve the "
            "Outlook rule blob, recheck previews and verify readback. Arbitrary condition/action "
            "editing is not exposed. Server/account access unprobed."
        ),
    ),
    _cap(
        "GetUserOofSettings",
        "automatic_replies",
        SafetyClass.READ_ONLY,
        verification=VerificationLevel.FIXTURE_TESTED,
        reason="Typed read through the account session; server/account access remains unprobed.",
    ),
    _cap(
        "SetUserOofSettings",
        "automatic_replies",
        SafetyClass.MUTATING,
        verification=VerificationLevel.FIXTURE_TESTED,
        reason=(
            "Reviewed policy, explicit confirmation, stale-preview protection and fresh readback. "
            "No mutation retry; server/account access remains unprobed."
        ),
    ),
    # Delegation and permissions
    _cap(
        "GetDelegate",
        "delegation",
        SafetyClass.READ_ONLY,
        verification=VerificationLevel.FIXTURE_TESTED,
        reason="Read-only discovery for the configured mailbox only, with a 1000-entry "
        "local safety limit. EWS returns one unpaged collection; server/account access "
        "remains unprobed. Delegate and folder-permission mutations remain unavailable.",
    ),
    _cap(
        "AddDelegate",
        "delegation",
        SafetyClass.PRIVILEGED,
        path=ImplementationPath.SOAP_TYPED,
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        reason="Privileged delegate mutation is not exposed by the safe client profile.",
    ),
    _cap(
        "UpdateDelegate",
        "delegation",
        SafetyClass.PRIVILEGED,
        path=ImplementationPath.SOAP_TYPED,
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        reason="Privileged delegate mutation is not exposed by the safe client profile.",
    ),
    _cap(
        "RemoveDelegate",
        "delegation",
        SafetyClass.PRIVILEGED,
        path=ImplementationPath.SOAP_TYPED,
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        reason="Privileged delegate mutation is not exposed by the safe client profile.",
    ),
    _catalog_only(
        "GetFolderPermissions",
        "permissions",
        SafetyClass.PRIVILEGED,
        reason="Folder-permission discovery is not exposed by the current safe adapter.",
    ),
    _catalog_only(
        "UpdateFolderPermissions",
        "permissions",
        SafetyClass.PRIVILEGED,
        reason="Folder-permission mutation is not exposed by the current safe adapter.",
    ),
    # User configuration and metadata
    _catalog_only(
        "GetUserConfiguration",
        "user_configuration",
        SafetyClass.READ_ONLY,
        minimum_version="Exchange2010",
        reason="Mailbox user-configuration blobs are not exposed by the safe adapter.",
    ),
    _catalog_only(
        "CreateUserConfiguration",
        "user_configuration",
        SafetyClass.MUTATING,
        minimum_version="Exchange2010",
        reason="Mailbox user-configuration mutation is not exposed by the safe adapter.",
    ),
    _catalog_only(
        "UpdateUserConfiguration",
        "user_configuration",
        SafetyClass.MUTATING,
        minimum_version="Exchange2010",
        reason="Mailbox user-configuration mutation is not exposed by the safe adapter.",
    ),
    _catalog_only(
        "DeleteUserConfiguration",
        "user_configuration",
        SafetyClass.DESTRUCTIVE,
        minimum_version="Exchange2010",
        reason="Mailbox user-configuration deletion is not exposed by the safe adapter.",
    ),
    _catalog_only(
        "GetMailTips",
        "mail_tips",
        SafetyClass.READ_ONLY,
        minimum_version="Exchange2010",
        reason="MailTips lookup is not exposed by the current safe adapter.",
    ),
    _cap(
        "GetPasswordExpirationDate",
        "account",
        SafetyClass.READ_ONLY,
        path=ImplementationPath.SOAP_TYPED,
        minimum_version="Exchange2010_SP1",
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        reason="Password-expiration discovery has no typed adapter yet.",
    ),
    _cap(
        "GetServiceConfiguration",
        "account",
        SafetyClass.READ_ONLY,
        path=ImplementationPath.SOAP_TYPED,
        minimum_version="Exchange2010",
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        reason="Service-configuration discovery has no typed adapter yet.",
    ),
    _cap(
        "GetRetentionPolicyTags",
        "retention",
        SafetyClass.READ_ONLY,
        path=ImplementationPath.SOAP_TYPED,
        minimum_version="Exchange2013",
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        reason="Retention-tag discovery has no typed adapter yet.",
    ),
    # Native item sync is opt-in; subscriptions and hierarchy sync remain
    # discoverable without claiming adapters or push transports that do not exist.
    _catalog_only(
        "SyncFolderHierarchy",
        "synchronization",
        SafetyClass.READ_ONLY,
        fallback="Rediscover the bounded folder tree when a refresh is requested.",
    ),
    _cap(
        "SyncFolderItems",
        "synchronization",
        SafetyClass.READ_ONLY,
        fallback="Use checkpointed, bounded timestamp polling with overlap deduplication.",
        verification=VerificationLevel.FIXTURE_TESTED,
        reason="Opt-in metadata-only mail/calendar change pages with durable quiet baselines.",
    ),
    _catalog_only(
        "SubscribePull",
        "notifications",
        SafetyClass.READ_ONLY,
        fallback="Use conservative checkpointed polling.",
    ),
    _catalog_only(
        "SubscribeStreaming",
        "notifications",
        SafetyClass.READ_ONLY,
        minimum_version="Exchange2010_SP1",
        fallback="Use conservative checkpointed polling.",
    ),
    _catalog_only(
        "SubscribePush",
        "notifications",
        SafetyClass.READ_ONLY,
        fallback=(
            "Use local outbound notifications after conservative polling; no inbound "
            "public callback server is started."
        ),
    ),
    _catalog_only(
        "GetEvents",
        "notifications",
        SafetyClass.READ_ONLY,
        fallback="Use conservative checkpointed polling.",
    ),
    _catalog_only(
        "GetStreamingEvents",
        "notifications",
        SafetyClass.READ_ONLY,
        minimum_version="Exchange2010_SP1",
        fallback="Use conservative checkpointed polling.",
    ),
    _catalog_only(
        "Unsubscribe",
        "notifications",
        SafetyClass.READ_ONLY,
        reason="The client does not create EWS subscriptions, so there is nothing to unsubscribe.",
    ),
    # Quota, archive and extended properties
    _cap(
        "MailboxQuota",
        "quota",
        SafetyClass.READ_ONLY,
        path=ImplementationPath.REGISTRY_ONLY,
        implemented=ImplementationStatus.PARTIAL,
        fallback=(
            "Sum folder sizes for usage and use the configured send limit. Report "
            "server-returned thresholds when present."
        ),
        reason="Standard EWS has no dependable used/quota operation for ordinary mailboxes.",
    ),
    _cap(
        "PidTagMessageSizeExtended",
        "quota",
        SafetyClass.READ_ONLY,
        path=ImplementationPath.EXTENDED_PROPERTY,
        implemented=ImplementationStatus.IMPLEMENTED,
        reason="Folder sizes are summed to show mailbox usage.",
    ),
    _cap(
        "PidTagDeferredSendTime",
        "scheduling",
        SafetyClass.MUTATING,
        path=ImplementationPath.EXTENDED_PROPERTY,
        implemented=ImplementationStatus.PARTIAL,
        fallback="Durable local scheduler",
        reason="Native deferred-send semantics must be verified on the detected server before use.",
    ),
    _cap(
        "NamedMapiProperties",
        "extended_properties",
        SafetyClass.MUTATING,
        path=ImplementationPath.EXTENDED_PROPERTY,
        implemented=ImplementationStatus.PARTIAL,
        fallback="Use the small set of explicitly registered, validated properties.",
        reason="Arbitrary named-property mutation is intentionally not exposed.",
    ),
    _catalog_only(
        "RecoverableItems",
        "recovery",
        SafetyClass.READ_ONLY,
        fallback="Deleted Items restore when recoverable-item access is unavailable.",
        reason=(
            "The client can move items out of Deleted Items, but it does not access the hidden "
            "Recoverable Items subtree."
        ),
    ),
    _cap(
        "ArchiveMailbox",
        "archive",
        SafetyClass.READ_ONLY,
        implemented=ImplementationStatus.PARTIAL,
        fallback="Normal archive folder when present.",
    ),
    # Administrative/discovery operations remain visible but are not exposed as ordinary actions.
    _cap(
        "GetSearchableMailboxes",
        "discovery",
        SafetyClass.PRIVILEGED,
        path=ImplementationPath.REGISTRY_ONLY,
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        access=AccountAccess.ADMIN_ONLY,
        reason="eDiscovery operation requiring administrative roles.",
    ),
    _cap(
        "SearchMailboxes",
        "discovery",
        SafetyClass.PRIVILEGED,
        path=ImplementationPath.REGISTRY_ONLY,
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        access=AccountAccess.ADMIN_ONLY,
        reason="eDiscovery operation requiring administrative roles.",
    ),
    _cap(
        "GetDiscoverySearchConfiguration",
        "discovery",
        SafetyClass.PRIVILEGED,
        path=ImplementationPath.REGISTRY_ONLY,
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        access=AccountAccess.ADMIN_ONLY,
        reason="Administrative discovery configuration.",
    ),
    _cap(
        "GetHoldOnMailboxes",
        "discovery",
        SafetyClass.PRIVILEGED,
        path=ImplementationPath.REGISTRY_ONLY,
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        access=AccountAccess.ADMIN_ONLY,
        reason="Administrative litigation-hold operation.",
    ),
    _cap(
        "SetHoldOnMailboxes",
        "discovery",
        SafetyClass.PRIVILEGED,
        path=ImplementationPath.REGISTRY_ONLY,
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        access=AccountAccess.ADMIN_ONLY,
        reason="Administrative litigation-hold mutation.",
    ),
    _cap(
        "GetAppManifests",
        "applications",
        SafetyClass.PRIVILEGED,
        path=ImplementationPath.REGISTRY_ONLY,
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        access=AccountAccess.ADMIN_ONLY,
        reason="Not relevant to mailbox-client workflows.",
    ),
    _cap(
        "InstallApp",
        "applications",
        SafetyClass.PRIVILEGED,
        path=ImplementationPath.REGISTRY_ONLY,
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        access=AccountAccess.ADMIN_ONLY,
        reason="Administrative application installation is outside the safe client profile.",
    ),
    _cap(
        "UninstallApp",
        "applications",
        SafetyClass.PRIVILEGED,
        path=ImplementationPath.REGISTRY_ONLY,
        implemented=ImplementationStatus.NOT_IMPLEMENTED,
        access=AccountAccess.ADMIN_ONLY,
        reason="Administrative application removal is outside the safe client profile.",
    ),
)


class CapabilityRegistry:
    """Static support truth plus optional conservative live detection results."""

    def __init__(self, capabilities: Iterable[Capability] = _OPERATIONS) -> None:
        items = tuple(capabilities)
        self._by_name = {item.operation.casefold(): item for item in items}
        if len(items) != len(self._by_name):
            raise ValueError("Capability operation names must be unique.")

    def all(self) -> tuple[Capability, ...]:
        return tuple(sorted(self._by_name.values(), key=lambda item: (item.family, item.operation)))

    def get(self, operation: str) -> Capability:
        try:
            return self._by_name[operation.casefold()]
        except KeyError as exc:
            raise KeyError(f"Unknown EWS operation: {operation}") from exc

    def by_family(self, family: str) -> tuple[Capability, ...]:
        return tuple(item for item in self.all() if item.family.casefold() == family.casefold())

    def with_detection(
        self,
        operation: str,
        *,
        server_support: ServerSupport,
        account_access: AccountAccess = AccountAccess.NOT_PROBED,
        verification: VerificationLevel = VerificationLevel.STATIC,
        reason: str | None = None,
    ) -> Capability:
        current = self.get(operation)
        if server_support is ServerSupport.UNSUPPORTED_VERSION:
            status = CapabilityStatus.SERVER_UNSUPPORTED
        elif server_support is ServerSupport.SERVER_REJECTED:
            status = CapabilityStatus.CONFIG_DISABLED
        elif account_access is AccountAccess.UNAUTHORIZED:
            status = CapabilityStatus.UNAUTHORIZED
        elif current.implementation_status is ImplementationStatus.IMPLEMENTED and verification in {
            VerificationLevel.LIVE_READ,
            VerificationLevel.LIVE_MUTATION,
        }:
            status = CapabilityStatus.LIVE_VERIFIED
        else:
            status = current.status
        updated = replace(
            current,
            server_support=server_support,
            account_access=account_access,
            verification=verification,
            status=status,
            reason=reason or current.reason,
        )
        self._by_name[operation.casefold()] = updated
        return updated


DEFAULT_REGISTRY = CapabilityRegistry()
