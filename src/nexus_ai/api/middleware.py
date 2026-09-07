"""Request context and security-header ASGI middleware (NXS-CTX-001, NXS-LOG-001).

Pure ASGI (not ``BaseHTTPMiddleware``) so request-scoped context propagates cleanly
through every downstream ``await``. Each request gets a validated/generated request id
and correlation id, echoed on the response and bound into the log context; an access
log line is emitted with method, path, status and duration — never headers or bodies.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from nexus_ai.core.config import Settings
from nexus_ai.core.context import request_context
from nexus_ai.core.identifiers import sanitize
from nexus_ai.core.logging import get_logger
from nexus_ai.core.telemetry import current_trace_id

_SEND = Callable[[Message], Awaitable[None]]


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self._app = app
        self._request_header = settings.http.request_id_header
        self._correlation_header = settings.http.correlation_id_header
        self._max_header_bytes = settings.http.max_request_header_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        raw_header_bytes = sum(len(name) + len(value) for name, value in scope["headers"])
        headers = Headers(scope=scope)
        incoming_request_id = headers.get(self._request_header)
        request_id = sanitize(incoming_request_id)
        correlation_id = sanitize(headers.get(self._correlation_header) or request_id)
        trace_id = current_trace_id()

        status_code = 500
        response_started = False
        logger = get_logger("nexus_ai.access")

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                response_headers = MutableHeaders(scope=message)
                response_headers[self._request_header] = request_id
                response_headers[self._correlation_header] = correlation_id
            await send(message)

        started = time.perf_counter()
        with (
            request_context(
                request_id=request_id, correlation_id=correlation_id, trace_id=trace_id
            ),
            structlog.contextvars.bound_contextvars(
                request_id=request_id, correlation_id=correlation_id
            ),
        ):
            if raw_header_bytes > self._max_header_bytes:
                await _reject_oversized_headers(send_wrapper)
                status_code = 431
            else:
                try:
                    await self._app(scope, receive, send_wrapper)
                except Exception:
                    if response_started:
                        raise
                    status_code = 500
                    await _emit_unexpected(send_wrapper, scope["path"])
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            await logger.ainfo(
                "http_request",
                method=scope["method"],
                path=scope["path"],
                status=status_code,
                duration_ms=duration_ms,
            )


async def _emit_unexpected(send: _SEND, path: str) -> None:
    from nexus_ai.core.problem_details import PROBLEM_MEDIA_TYPE, unexpected

    await get_logger("nexus_ai.error").aerror("unhandled_exception")
    problem = unexpected(instance=path)
    body = problem.model_dump_json(exclude_none=True).encode()
    await send(
        {
            "type": "http.response.start",
            "status": problem.status,
            "headers": [(b"content-type", PROBLEM_MEDIA_TYPE.encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _reject_oversized_headers(send: _SEND) -> None:
    body = b'{"title":"Request Header Fields Too Large","status":431,'
    body += b'"detail":"Request headers exceed the configured limit.",'
    body += b'"code":"NXS_CORE_INVALID_REQUEST"}'
    await send(
        {
            "type": "http.response.start",
            "status": 431,
            "headers": [(b"content-type", b"application/problem+json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware:
    """Conservative security headers for an API served behind a TLS edge."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self._app = app
        self._headers: list[tuple[bytes, bytes]] = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
            (b"cross-origin-opener-policy", b"same-origin"),
            (b"cache-control", b"no-store"),
        ]
        if hsts:
            self._headers.append(
                (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
            )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                for name, value in self._headers:
                    response_headers.setdefault(name.decode(), value.decode())
                response_headers["server"] = "nexus-ai"
            await send(message)

        await self._app(scope, receive, send_wrapper)
