"""The governed model HTTP transport (NXS-P13): the only outbound path from
``nexus_ai.agents``, wrapping the NXS-P07 governed executor."""

from __future__ import annotations

from typing import Any

import pytest

from nexus_ai.agents.models.base import HttpError
from nexus_ai.agents.models.transport import GovernedModelHttpTransport
from nexus_ai.integrations.backoff import FailureKind
from nexus_ai.integrations.executor import ExecutorFailure, OutboundRequest, RawResponse

pytestmark = pytest.mark.anyio


class _FakeExecutor:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.request: OutboundRequest | None = None

    async def send(self, request: OutboundRequest) -> RawResponse:
        self.request = request
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


async def test_a_successful_call_maps_the_raw_response_and_clamps_timeout() -> None:
    executor = _FakeExecutor(
        RawResponse(200, {"content-type": "application/json"}, b'{"ok":1}', 5, "https://m/x")
    )
    transport = GovernedModelHttpTransport(executor, timeout_seconds=10.0)  # type: ignore[arg-type]
    response = await transport.request(
        method="POST",
        url="https://models.example/v1/chat/completions",
        headers={"Authorization": "Bearer sk", "Content-Type": "application/json"},
        body=b"{}",
        timeout_seconds=999.0,
    )
    assert response.status_code == 200 and response.body == b'{"ok":1}'
    assert executor.request is not None
    assert executor.request.timeout_seconds == 10.0  # clamped to the transport ceiling
    assert executor.request.content_type == "application/json"


async def test_an_executor_timeout_failure_becomes_a_timeout_http_error() -> None:
    boom = ExecutorFailure(FailureKind.IN_FLIGHT, RuntimeError("the upstream request timed out"))
    transport = GovernedModelHttpTransport(_FakeExecutor(boom), timeout_seconds=5.0)  # type: ignore[arg-type]
    with pytest.raises(HttpError) as excinfo:
        await transport.request(method="POST", url="https://m/x", headers={}, body=None)
    assert excinfo.value.timeout is True and excinfo.value.connect is False


async def test_an_executor_connect_failure_becomes_a_connect_http_error() -> None:
    boom = ExecutorFailure(FailureKind.PRE_SEND, RuntimeError("connection refused"))
    transport = GovernedModelHttpTransport(_FakeExecutor(boom), timeout_seconds=5.0)  # type: ignore[arg-type]
    with pytest.raises(HttpError) as excinfo:
        await transport.request(method="GET", url="https://m/x", headers={}, body=None)
    assert excinfo.value.timeout is False and excinfo.value.connect is True
