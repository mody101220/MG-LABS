"""Fixed error-code set and HTTP mapping (contract §29). Do not extend without a spec revision."""
from __future__ import annotations

from typing import Any

from app.generated import models


class AppError(Exception):
    """Contract error: code + HTTP status + details. Rendered into the error envelope."""

    def __init__(self, code: models.ErrorCode, message: str, details: dict[str, Any] | None = None, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.status = status or STATUS_BY_CODE.get(code, 500)


STATUS_BY_CODE: dict[models.ErrorCode, int] = {
    models.ErrorCode.VALIDATION_ERROR: 400,
    models.ErrorCode.AUTHENTICATION_FAILED: 401,
    models.ErrorCode.INVALID_TOKEN: 401,
    models.ErrorCode.PERMISSION_DENIED: 403,
    models.ErrorCode.SECRET_ACCESS_DENIED: 403,
    models.ErrorCode.RESOURCE_NOT_FOUND: 404,
    models.ErrorCode.CONFLICT: 409,
    models.ErrorCode.APPROVAL_REQUIRED: 409,
    models.ErrorCode.RATE_LIMITED: 429,
    models.ErrorCode.EXECUTION_FAILED: 500,
    models.ErrorCode.VERIFICATION_FAILED: 500,
    models.ErrorCode.DEPENDENCY_FAILURE: 500,
    models.ErrorCode.INTEGRATION_OFFLINE: 503,
    models.ErrorCode.TOOL_UNAVAILABLE: 503,
    models.ErrorCode.TIMEOUT: 504,
}

# Generic, safe messages — never expose internals.
GENERIC_MESSAGES = {
    models.ErrorCode.AUTHENTICATION_FAILED: "Authentication failed",
    models.ErrorCode.INVALID_TOKEN: "Token is invalid or expired",
    models.ErrorCode.PERMISSION_DENIED: "Required permission is missing",
    models.ErrorCode.SECRET_ACCESS_DENIED: "Access to a secret resource is denied",
    models.ErrorCode.VALIDATION_ERROR: "Request failed validation",
    models.ErrorCode.RESOURCE_NOT_FOUND: "Resource not found",
    models.ErrorCode.CONFLICT: "Resource state conflict",
    models.ErrorCode.APPROVAL_REQUIRED: "Human approval is required before proceeding",
    models.ErrorCode.RATE_LIMITED: "Rate limit exceeded, retry after the reset",
    models.ErrorCode.INTEGRATION_OFFLINE: "External integration is not connected",
    models.ErrorCode.TOOL_UNAVAILABLE: "Tool is not available right now",
    models.ErrorCode.EXECUTION_FAILED: "Execution failed",
    models.ErrorCode.VERIFICATION_FAILED: "Verification of the result failed",
    models.ErrorCode.DEPENDENCY_FAILURE: "Internal dependency failure",
    models.ErrorCode.TIMEOUT: "Operation timed out",
}


def validation_error(details: dict[str, Any] | None = None, message: str | None = None) -> AppError:
    return AppError(models.ErrorCode.VALIDATION_ERROR, message or GENERIC_MESSAGES[models.ErrorCode.VALIDATION_ERROR], details)


def not_found(resource: str) -> AppError:
    return AppError(models.ErrorCode.RESOURCE_NOT_FOUND, f"{resource} not found", {"resource": resource})


def forbidden(required: str | None = None) -> AppError:
    details = {"required": required} if required else {}
    return AppError(models.ErrorCode.PERMISSION_DENIED, GENERIC_MESSAGES[models.ErrorCode.PERMISSION_DENIED], details)


def approval_required(approval_id: str, extra: dict[str, Any] | None = None) -> AppError:
    details: dict[str, Any] = {"approval_id": approval_id}
    if extra:
        details.update(extra)
    return AppError(models.ErrorCode.APPROVAL_REQUIRED, "Human approval is required before proceeding", details)


def conflict(message: str, details: dict[str, Any] | None = None) -> AppError:
    return AppError(models.ErrorCode.CONFLICT, message, details)


def rate_limited() -> AppError:
    return AppError(models.ErrorCode.RATE_LIMITED, GENERIC_MESSAGES[models.ErrorCode.RATE_LIMITED])


def integration_offline(provider: str) -> AppError:
    return AppError(
        models.ErrorCode.INTEGRATION_OFFLINE,
        f"{provider} integration is not connected",
        {"provider": provider},
    )


def tool_unavailable(tool: str) -> AppError:
    return AppError(
        models.ErrorCode.TOOL_UNAVAILABLE,
        f"Tool '{tool}' is not available right now",
        {"tool": tool},
    )


def internal_error() -> AppError:
    return AppError(models.ErrorCode.EXECUTION_FAILED, GENERIC_MESSAGES[models.ErrorCode.EXECUTION_FAILED])


def invalid_token(message: str | None = None) -> AppError:
    return AppError(models.ErrorCode.INVALID_TOKEN, message or GENERIC_MESSAGES[models.ErrorCode.INVALID_TOKEN])


def authentication_failed(message: str | None = None) -> AppError:
    return AppError(models.ErrorCode.AUTHENTICATION_FAILED, message or GENERIC_MESSAGES[models.ErrorCode.AUTHENTICATION_FAILED])


def execution_failed(message: str, details: dict[str, Any] | None = None) -> AppError:
    return AppError(models.ErrorCode.EXECUTION_FAILED, message, details)
