"""Structured error types.

Every failure surfaced to a user, an Agent or a MCP client carries a stable
machine-readable ``code``.  Callers are expected to branch on the code, never on
the message text.
"""

from __future__ import annotations

from typing import Any


class RcfError(Exception):
    """Base class for all RCF errors."""

    code: str = "rcf_error"
    exit_code: int = 1

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.detail: dict[str, Any] = detail or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
        if self.detail:
            payload["detail"] = self.detail
        return payload

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class ConfigError(RcfError):
    code = "config_invalid"
    exit_code = 2


class SpecError(RcfError):
    """A declarative spec (node/project/environment) failed validation."""

    code = "spec_invalid"
    exit_code = 2


class PlanExpired(RcfError):
    """The plan is older than ``[runs].plan_ttl_minutes``."""

    code = "plan_expired"
    exit_code = 4


class NotFound(RcfError):
    code = "not_found"
    exit_code = 4


class HostUnreachable(RcfError):
    code = "host_unreachable"
    exit_code = 5


class AuthFailed(RcfError):
    code = "auth_failed"
    exit_code = 5


class HostKeyRejected(RcfError):
    code = "host_key_rejected"
    exit_code = 5


class ExecutableMissing(RcfError):
    code = "executable_missing"
    exit_code = 6


class CommandTimedOut(RcfError):
    code = "command_timeout"
    exit_code = 6


class PermissionDenied(RcfError):
    code = "permission_denied"
    exit_code = 7


class ApprovalRequired(RcfError):
    code = "approval_required"
    exit_code = 7


class Conflict(RcfError):
    code = "conflict"
    exit_code = 8


class SchedulerError(RcfError):
    code = "scheduler_error"
    exit_code = 9


class ExecutorError(RcfError):
    code = "executor_error"
    exit_code = 9


class SubmissionUncertain(SchedulerError):
    """Dispatch may have succeeded, but no trustworthy receipt was obtained."""

    code = "submission_uncertain"
    exit_code = 9


class StoreError(RcfError):
    code = "store_error"
    exit_code = 10


class UnsupportedOperation(RcfError):
    code = "unsupported_operation"
    exit_code = 11
