"""Exception handlers that render every failure as RFC 9457 Problem Details.

Normalises application errors, request-validation errors, 404, 405 and unexpected
exceptions. Stack traces never reach the client; unexpected errors are logged with the
request/correlation id for internal diagnosis.
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from nexus_ai.core import problem_details
from nexus_ai.core.errors import (
    MethodNotAllowedError,
    NotFoundError,
    NxsError,
    ValidationFailedError,
)
from nexus_ai.core.logging import get_logger
from nexus_ai.core.problem_details import PROBLEM_MEDIA_TYPE, ProblemDetails

_STATUS_ERRORS: dict[int, type[NxsError]] = {
    404: NotFoundError,
    405: MethodNotAllowedError,
}


def _response(problem: ProblemDetails) -> JSONResponse:
    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(mode="json", exclude_none=True),
        media_type=PROBLEM_MEDIA_TYPE,
    )


async def _handle_nxs_error(request: Request, exc: Exception) -> JSONResponse:
    error = cast(NxsError, exc)
    if error.status >= 500:
        await get_logger("nexus_ai.error").aerror(
            "unhandled_application_error", code=error.code, detail=error.detail
        )
    return _response(problem_details.from_error(error, instance=request.url.path))


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    validation = cast(RequestValidationError, exc)
    errors: list[dict[str, Any]] = []
    for raw in validation.errors()[:50]:
        location = raw.get("loc", ())
        errors.append(
            {
                "field": ".".join(str(part) for part in location) or "<request>",
                "message": str(raw.get("msg", "invalid value"))[:200],
                "type": str(raw.get("type", "value_error")),
            }
        )
    normalized = ValidationFailedError(errors)
    return _response(problem_details.from_error(normalized, instance=request.url.path))


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    http_exc = cast(StarletteHTTPException, exc)
    error_type = _STATUS_ERRORS.get(http_exc.status_code)
    if error_type is not None:
        detail = http_exc.detail if isinstance(http_exc.detail, str) else error_type.title
        return _response(problem_details.from_error(error_type(detail), instance=request.url.path))
    detail = (
        http_exc.detail if isinstance(http_exc.detail, str) else "Request could not be completed."
    )
    return _response(
        problem_details.generic(
            status=http_exc.status_code,
            title=detail if http_exc.status_code < 500 else "Internal Server Error",
            detail=detail,
            code="NXS_CORE_HTTP_ERROR",
            instance=request.url.path,
        )
    )


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    await get_logger("nexus_ai.error").aerror("unexpected_exception", error_type=type(exc).__name__)
    return _response(problem_details.unexpected(instance=request.url.path))


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(NxsError, _handle_nxs_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected)
