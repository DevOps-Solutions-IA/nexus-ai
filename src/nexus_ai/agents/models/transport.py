"""The governed model HTTP transport (NXS-P13).

Wraps the NXS-P07 governed HTTP executor so every model provider REST call is SSRF-safe,
TLS-verified and bounded — a model adapter can never reach an arbitrary or internal URL,
and the model itself never influences the destination. There is no other outbound path
from ``nexus_ai.agents``.
"""

from __future__ import annotations

from nexus_ai.agents.models.base import HttpError, HttpResponse
from nexus_ai.integrations.executor import GovernedHttpExecutor, OutboundRequest


class GovernedModelHttpTransport:
    def __init__(self, executor: GovernedHttpExecutor, *, timeout_seconds: float) -> None:
        self._executor = executor
        self._timeout = timeout_seconds

    async def request(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout_seconds: float | None = None,
    ) -> HttpResponse:
        from nexus_ai.integrations.executor import ExecutorFailure

        effective = (
            self._timeout if timeout_seconds is None else min(self._timeout, timeout_seconds)
        )
        try:
            raw = await self._executor.send(
                OutboundRequest(
                    method=method,
                    url=url,
                    headers=headers,
                    body=body,
                    content_type=headers.get("Content-Type"),
                    timeout_seconds=effective,
                )
            )
        except ExecutorFailure as exc:
            message = str(exc.public)
            timed_out = "timeout" in message.lower() or "timed out" in message.lower()
            raise HttpError(message, timeout=timed_out, connect=not timed_out) from exc
        return HttpResponse(status_code=raw.status_code, headers=dict(raw.headers), body=raw.body)
