"""Provider adapter registry + the governed telephony transport (NXS-P11)."""

from __future__ import annotations

from nexus_ai.integrations.executor import GovernedHttpExecutor, OutboundRequest
from nexus_ai.telephony.entities import TelephonyProvider
from nexus_ai.telephony.providers.asterisk import AsteriskAdapter
from nexus_ai.telephony.providers.base import (
    TelephonyProviderAdapter,
    TransportError,
    TransportResponse,
)
from nexus_ai.telephony.providers.fake import FakeTelephonyProvider

_ADAPTERS: dict[TelephonyProvider, TelephonyProviderAdapter] = {
    TelephonyProvider.ASTERISK: AsteriskAdapter(),
    TelephonyProvider.FAKE: FakeTelephonyProvider(),
}


def resolve_provider(provider: TelephonyProvider) -> TelephonyProviderAdapter:
    return _ADAPTERS[provider]


def known_providers() -> tuple[str, ...]:
    return tuple(sorted(p.value for p in _ADAPTERS))


class GovernedTelephonyTransport:
    """Wraps the NXS-P07 governed HTTP executor so every provider / ARI call is
    SSRF-safe, TLS-verified and bounded — a telephony adapter can never reach an
    arbitrary or internal URL."""

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
    ) -> TransportResponse:
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
            raise TransportError(message, timeout=timed_out, connect=not timed_out) from exc
        return TransportResponse(
            status_code=raw.status_code, headers=dict(raw.headers), body=raw.body
        )
