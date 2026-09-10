"""Voice provider adapter registry + the governed voice HTTP transport (NXS-P12)."""

from __future__ import annotations

from nexus_ai.integrations.executor import GovernedHttpExecutor, OutboundRequest
from nexus_ai.voice.entities import VoiceProvider
from nexus_ai.voice.providers.base import HttpError, HttpResponse, VoiceProviderAdapter
from nexus_ai.voice.providers.elevenlabs import ElevenLabsVoiceAdapter
from nexus_ai.voice.providers.fake import FakeVoiceProvider

_ADAPTERS: dict[VoiceProvider, VoiceProviderAdapter] = {
    VoiceProvider.ELEVENLABS: ElevenLabsVoiceAdapter(),
    VoiceProvider.FAKE: FakeVoiceProvider(),
}


def resolve_voice_provider(provider: VoiceProvider) -> VoiceProviderAdapter:
    return _ADAPTERS[provider]


def known_voice_providers() -> tuple[str, ...]:
    return tuple(sorted(p.value for p in _ADAPTERS))


class GovernedVoiceHttpTransport:
    """Wraps the NXS-P07 governed HTTP executor so every voice provider REST call is
    SSRF-safe, TLS-verified and bounded — a voice adapter can never reach an arbitrary or
    internal URL. The real-time WebSocket is handled separately by
    :class:`~nexus_ai.voice.transport.WebsocketVoiceStreamTransport`."""

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
