"""Unified success/error envelope (contract §2).

Success: {success: true, data, request_id, timestamp}
Error:   {success: false, error: {code, message, details}, request_id, timestamp}
"""
from __future__ import annotations

import logging
from typing import Any, TypeVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core import context
from app.core.errors import AppError, GENERIC_MESSAGES, STATUS_BY_CODE
from app.generated import models

M = TypeVar("M", bound=models.BaseModel)


def success_payload(data: Any, request_id: str) -> dict[str, Any]:
    return {
        "success": True,
        "data": data,
        "request_id": request_id,
        "timestamp": context.now_utc().isoformat().replace("+00:00", "Z"),
    }


def error_payload(code: models.ErrorCode, message: str, details: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {
        "success": False,
        "error": {"code": code.value, "message": message, "details": details},
        "request_id": request_id,
        "timestamp": context.now_utc().isoformat().replace("+00:00", "Z"),
    }


def build_success(model_cls: type[M], data: Any) -> M:
    """Construct a generated success response model with the live request context."""
    return model_cls(
        success=models.Success.boolean_True,
        data=data,
        request_id=context.request_id_var.get() or context.new_request_id(),
        timestamp=context.now_utc(),
    )


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        context.log_event("request.error", {"code": exc.code.value, "status": exc.status}, logging.WARNING)
        return JSONResponse(status_code=exc.status, content=error_payload(exc.code, exc.message, exc.details, context.request_id_var.get() or context.new_request_id()))

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields: dict[str, list[str]] = {}
        for err in exc.errors():
            loc = [str(p) for p in err.get("loc", []) if p not in ("body", "query", "header", "path")]
            key = ".".join(loc) or "request"
            fields.setdefault(key, []).append(err.get("msg", "invalid"))
        return JSONResponse(status_code=400, content=error_payload(
            models.ErrorCode.VALIDATION_ERROR,
            "Request failed validation",
            {"fields": fields},
            context.request_id_var.get() or context.new_request_id(),
        ))

    @app.exception_handler(StarletteHTTPException)
    async def http_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        # 404/405 for unknown routes and a few framework paths; keep the contract shape.
        if exc.status_code == 404:
            code, message = models.ErrorCode.RESOURCE_NOT_FOUND, "Resource not found"
        elif exc.status_code == 405:
            code, message = models.ErrorCode.VALIDATION_ERROR, "Method not allowed on this path"
        elif exc.status_code == 401:
            code, message = models.ErrorCode.INVALID_TOKEN, GENERIC_MESSAGES[models.ErrorCode.INVALID_TOKEN]
        else:
            code, message = models.ErrorCode.EXECUTION_FAILED, GENERIC_MESSAGES[models.ErrorCode.EXECUTION_FAILED]
        status = STATUS_BY_CODE.get(code, 500) if exc.status_code >= 500 else exc.status_code
        return JSONResponse(status_code=status, content=error_payload(code, message, {}, context.request_id_var.get() or context.new_request_id()))

    @app.exception_handler(Exception)
    async def unhandled_handler(_: Request, exc: Exception) -> JSONResponse:
        # Log the full traceback server-side ONLY. The client sees a generic message.
        context.log_event("unhandled.exception", {"type": type(exc).__name__}, logging.ERROR)
        logging.getLogger("ghayath").exception("unhandled exception")
        return JSONResponse(status_code=500, content=error_payload(
            models.ErrorCode.EXECUTION_FAILED,
            GENERIC_MESSAGES[models.ErrorCode.EXECUTION_FAILED],
            {},
            context.request_id_var.get() or context.new_request_id(),
        ))
