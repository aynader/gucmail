"""Stable application errors and process exit codes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    """Documented process exit statuses."""

    OK = 0
    USAGE = 2
    CONFIGURATION = 3
    AUTHENTICATION = 4
    NETWORK = 5
    NOT_FOUND = 6
    CONFLICT = 7
    UNSUPPORTED = 8
    CONFIRMATION_REQUIRED = 9
    PARTIAL = 10
    LOCAL_STATE = 11
    SERVER = 12
    INTERRUPTED = 130


@dataclass(slots=True)
class ErrorContext:
    """Safe detail presented to a user without leaking server internals."""

    retry_safe: bool | None = None
    mutation_uncertain: bool = False
    suggestion: str | None = None
    correlation_id: str | None = None


class GUCmailError(Exception):
    """Base class for errors whose message is safe for normal output."""

    exit_code = ExitCode.SERVER

    def __init__(self, message: str, *, context: ErrorContext | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context = context or ErrorContext()

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": type(self).__name__,
            "message": self.message,
            "exit_code": int(self.exit_code),
            "retry_safe": self.context.retry_safe,
            "mutation_uncertain": self.context.mutation_uncertain,
            "suggestion": self.context.suggestion,
            "correlation_id": self.context.correlation_id,
        }


class ConfigurationError(GUCmailError):
    exit_code = ExitCode.CONFIGURATION


class MutationExecutionError(GUCmailError):
    """A stopped batch retains the exact known/uncertain effect of each target."""

    exit_code = ExitCode.PARTIAL

    def __init__(
        self,
        message: str,
        *,
        operation_id: str,
        operation: str,
        outcomes: tuple[dict[str, Any], ...],
        cause_type: str,
        cause_exit_code: ExitCode = ExitCode.SERVER,
        journal_available: bool = False,
    ) -> None:
        uncertain = any(item["status"] == "uncertain" for item in outcomes)
        changed = any(item["status"] == "completed" for item in outcomes)
        if not changed and not uncertain:
            self.exit_code = cause_exit_code
        super().__init__(
            message,
            context=ErrorContext(
                retry_safe=False if changed or uncertain else None,
                mutation_uncertain=uncertain,
                correlation_id=operation_id,
                suggestion=(
                    f"Review gucmail messages operations show {operation_id}. "
                    "Do not repeat completed or uncertain targets."
                )
                if journal_available
                else (
                    "Inspect the per-target outcomes before retrying. No durable operation "
                    "journal was configured; do not repeat completed or uncertain targets."
                ),
            ),
        )
        self.operation_id = operation_id
        self.operation = operation
        self.outcomes = outcomes
        self.cause_type = cause_type

    def as_dict(self) -> dict[str, Any]:
        return {
            **super().as_dict(),
            "operation_id": self.operation_id,
            "operation": self.operation,
            "outcomes": self.outcomes,
            "cause_type": self.cause_type,
        }


class CredentialError(GUCmailError):
    exit_code = ExitCode.AUTHENTICATION


class AuthenticationError(CredentialError):
    pass


class NetworkError(GUCmailError):
    exit_code = ExitCode.NETWORK


class ThrottledError(NetworkError):
    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(
            message,
            context=ErrorContext(
                retry_safe=True,
                suggestion="Wait for the server-requested back-off period, then retry once.",
            ),
        )
        self.retry_after = retry_after


class ItemNotFoundError(GUCmailError):
    exit_code = ExitCode.NOT_FOUND


class ConflictError(GUCmailError):
    exit_code = ExitCode.CONFLICT


class UnsupportedFeatureError(GUCmailError):
    exit_code = ExitCode.UNSUPPORTED


class ConfirmationRequiredError(GUCmailError):
    exit_code = ExitCode.CONFIRMATION_REQUIRED


class LocalStateError(GUCmailError):
    exit_code = ExitCode.LOCAL_STATE


class MutationResultUnavailableError(GUCmailError):
    """The provider accepted an update but its result could not be inspected."""

    exit_code = ExitCode.PARTIAL

    def __init__(self, message: str, *, suggestion: str) -> None:
        super().__init__(
            message,
            context=ErrorContext(
                retry_safe=False,
                mutation_uncertain=True,
                suggestion=suggestion,
            ),
        )


class SendOutcomeUncertainError(GUCmailError):
    """The connection failed after a send may have reached Exchange."""

    exit_code = ExitCode.CONFLICT

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            context=ErrorContext(
                retry_safe=False,
                mutation_uncertain=True,
                suggestion=(
                    "Do not send again yet. Use the outbox review command to reconcile against "
                    "Sent Items."
                ),
            ),
        )


class BackupIntegrityError(GUCmailError):
    exit_code = ExitCode.LOCAL_STATE
