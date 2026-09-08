"""Governed HTTP executor (NXS-INT-001, ADR-0056)."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from nexus_ai.core.config import IntegrationsSettings
from nexus_ai.integrations.backoff import FailureKind
from nexus_ai.integrations.destination import DestinationPolicy
from nexus_ai.integrations.errors import (
    IntegrationConnectionError,
    IntegrationRedirectBlockedError,
    IntegrationResponseTooLargeError,
    IntegrationTlsError,
)
from nexus_ai.integrations.executor import ExecutorFailure, GovernedHttpExecutor, OutboundRequest

pytestmark = pytest.mark.anyio


def _executor(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    settings: IntegrationsSettings | None = None,
) -> GovernedHttpExecutor:
    settings = settings or IntegrationsSettings()
    policy = DestinationPolicy(allow_loopback=True, resolver=lambda host, port: ["127.0.0.1"])

    def factory(timeout: httpx.Timeout) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

    return GovernedHttpExecutor(settings, policy, client_factory=factory)


async def test_success_returns_bounded_response() -> None:
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(200, json={"ok": True})

    executor = _executor(handler)
    resp = await executor.send(
        OutboundRequest(method="GET", url="https://api.integration.test/v1/x")
    )
    assert resp.status_code == 200 and b'"ok"' in resp.body
    assert captured["req"].headers["host"] == "api.integration.test"
    assert captured["req"].headers["user-agent"] == "NexusAI-IntegrationHub/1.0"


async def test_reserved_headers_are_stripped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("cookie") is None
        assert request.headers["x-app"] == "yes"
        return httpx.Response(204)

    await _executor(handler).send(
        OutboundRequest(
            method="GET",
            url="https://api.integration.test/x",
            headers={"Cookie": "sid=abc", "X-App": "yes"},
        )
    )


async def test_request_body_limit() -> None:
    executor = _executor(
        lambda r: httpx.Response(200), settings=IntegrationsSettings(max_request_bytes=1024)
    )
    with pytest.raises(IntegrationConnectionError, match="request body"):
        await executor.send(
            OutboundRequest(method="POST", url="https://api.integration.test/x", body=b"x" * 2048)
        )


async def test_response_too_large() -> None:
    executor = _executor(
        lambda r: httpx.Response(200, content=b"y" * 5000),
        settings=IntegrationsSettings(max_response_bytes=1024, max_request_bytes=2048),
    )
    with pytest.raises(IntegrationResponseTooLargeError):
        await executor.send(OutboundRequest(method="GET", url="https://api.integration.test/x"))


async def test_redirect_is_blocked_when_disabled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://api.integration.test/other"})

    with pytest.raises(IntegrationRedirectBlockedError):
        await _executor(handler).send(
            OutboundRequest(method="GET", url="https://api.integration.test/x")
        )


async def test_redirect_followed_and_revalidated_when_allowed() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url.path))
        if request.url.path == "/x":
            return httpx.Response(307, headers={"location": "/y"})
        return httpx.Response(200, json={"final": True})

    executor = _executor(handler, settings=IntegrationsSettings(max_redirects=2))
    resp = await executor.send(OutboundRequest(method="GET", url="https://api.integration.test/x"))
    assert resp.status_code == 200 and calls == ["/x", "/y"]


async def test_connection_error_is_pre_send() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ExecutorFailure) as excinfo:
        await _executor(handler).send(
            OutboundRequest(method="GET", url="https://api.integration.test/x")
        )
    assert excinfo.value.kind is FailureKind.PRE_SEND
    assert isinstance(excinfo.value.public, IntegrationConnectionError)


async def test_tls_error_is_terminal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("certificate verify failed")

    with pytest.raises(ExecutorFailure) as excinfo:
        await _executor(handler).send(
            OutboundRequest(method="GET", url="https://api.integration.test/x")
        )
    assert excinfo.value.kind is FailureKind.TERMINAL
    assert isinstance(excinfo.value.public, IntegrationTlsError)


async def test_read_timeout_is_in_flight() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ExecutorFailure) as excinfo:
        await _executor(handler).send(
            OutboundRequest(method="GET", url="https://api.integration.test/x")
        )
    assert excinfo.value.kind is FailureKind.IN_FLIGHT


async def test_malformed_header_value_rejected() -> None:
    with pytest.raises(IntegrationConnectionError):
        await _executor(lambda r: httpx.Response(200)).send(
            OutboundRequest(
                method="GET",
                url="https://api.integration.test/x",
                headers={"X-Bad": "line1\r\nInjected: 1"},
            )
        )
