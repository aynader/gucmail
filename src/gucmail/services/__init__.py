"""Provider-neutral high-level mailbox services shared by CLI and TUI surfaces."""

from gucmail.services._base import (
    MutationExecution,
    MutationPlan,
    MutationRisk,
    PageRequest,
    ServicePage,
)
from gucmail.services.attachments import (
    AttachmentSavePlan,
    AttachmentService,
    CollisionPolicy,
    DownloadProgress,
    SavedAttachment,
)
from gucmail.services.compose import (
    AttachmentSnapshot,
    ComposeIssue,
    ComposeKind,
    ComposePlan,
    ComposePreview,
    ComposeService,
    ComposeValidation,
    ComposeValidationError,
    IssueLevel,
    SendExecution,
)
from gucmail.services.folders import (
    FolderService,
    format_folder_path,
    parse_folder_path,
    validate_folder_name,
)
from gucmail.services.messages import MessageService
from gucmail.services.monitoring import MailboxMonitor, MonitoringService
from gucmail.services.notifications import NotificationDispatcher
from gucmail.services.quota import (
    KNOWN_GUC_PROHIBIT_SEND_BYTES,
    LocalDiskUsage,
    QuotaEvent,
    QuotaNoticeParser,
    QuotaObservation,
    QuotaOverrides,
    QuotaReport,
    QuotaService,
    QuotaStatus,
    parse_quota_notification,
    render_quota_report,
)
from gucmail.services.webhooks import (
    WebhookConfig,
    WebhookProvider,
    WebhookRegistry,
)

__all__ = [
    "KNOWN_GUC_PROHIBIT_SEND_BYTES",
    "AttachmentSavePlan",
    "AttachmentService",
    "AttachmentSnapshot",
    "CollisionPolicy",
    "ComposeIssue",
    "ComposeKind",
    "ComposePlan",
    "ComposePreview",
    "ComposeService",
    "ComposeValidation",
    "ComposeValidationError",
    "DownloadProgress",
    "FolderService",
    "IssueLevel",
    "LocalDiskUsage",
    "MailboxMonitor",
    "MessageService",
    "MonitoringService",
    "MutationExecution",
    "MutationPlan",
    "MutationRisk",
    "NotificationDispatcher",
    "PageRequest",
    "QuotaEvent",
    "QuotaNoticeParser",
    "QuotaObservation",
    "QuotaOverrides",
    "QuotaReport",
    "QuotaService",
    "QuotaStatus",
    "SavedAttachment",
    "SendExecution",
    "ServicePage",
    "WebhookConfig",
    "WebhookProvider",
    "WebhookRegistry",
    "format_folder_path",
    "parse_folder_path",
    "parse_quota_notification",
    "render_quota_report",
    "validate_folder_name",
]
