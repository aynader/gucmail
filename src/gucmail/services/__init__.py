"""Provider-neutral high-level mailbox services shared by CLI and TUI surfaces.

Names load on first use, so importing one service does not import them all.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_SOURCES = {
    "AttachmentSavePlan": "gucmail.services.attachments",
    "AttachmentService": "gucmail.services.attachments",
    "AttachmentSnapshot": "gucmail.services.compose",
    "CollisionPolicy": "gucmail.services.attachments",
    "ComposeIssue": "gucmail.services.compose",
    "ComposeKind": "gucmail.services.compose",
    "ComposePlan": "gucmail.services.compose",
    "ComposePreview": "gucmail.services.compose",
    "ComposeService": "gucmail.services.compose",
    "ComposeValidation": "gucmail.services.compose",
    "ComposeValidationError": "gucmail.services.compose",
    "DownloadProgress": "gucmail.services.attachments",
    "FolderService": "gucmail.services.folders",
    "IssueLevel": "gucmail.services.compose",
    "KNOWN_GUC_PROHIBIT_SEND_BYTES": "gucmail.services.quota",
    "LocalDiskUsage": "gucmail.services.quota",
    "MailboxMonitor": "gucmail.services.monitoring",
    "MessageService": "gucmail.services.messages",
    "MonitoringService": "gucmail.services.monitoring",
    "MutationExecution": "gucmail.services._base",
    "MutationPlan": "gucmail.services._base",
    "MutationRisk": "gucmail.services._base",
    "NotificationDispatcher": "gucmail.services.notifications",
    "PageRequest": "gucmail.services._base",
    "QuotaEvent": "gucmail.services.quota",
    "QuotaNoticeParser": "gucmail.services.quota",
    "QuotaObservation": "gucmail.services.quota",
    "QuotaOverrides": "gucmail.services.quota",
    "QuotaReport": "gucmail.services.quota",
    "QuotaService": "gucmail.services.quota",
    "QuotaStatus": "gucmail.services.quota",
    "SavedAttachment": "gucmail.services.attachments",
    "SendExecution": "gucmail.services.compose",
    "ServicePage": "gucmail.services._base",
    "WebhookConfig": "gucmail.services.webhooks",
    "WebhookProvider": "gucmail.services.webhooks",
    "WebhookRegistry": "gucmail.services.webhooks",
    "format_folder_path": "gucmail.services.folders",
    "parse_folder_path": "gucmail.services.folders",
    "parse_quota_notification": "gucmail.services.quota",
    "render_quota_report": "gucmail.services.quota",
    "validate_folder_name": "gucmail.services.folders",
}

__all__ = sorted(_SOURCES)


def __getattr__(name: str) -> Any:
    if name in _SOURCES:
        return getattr(import_module(_SOURCES[name]), name)
    raise AttributeError(f"module 'gucmail.services' has no attribute {name!r}")
