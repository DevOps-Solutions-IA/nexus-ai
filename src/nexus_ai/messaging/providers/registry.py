"""Provider resolution + the governed HTTP transport (NXS-P09).

``GovernedMessagingTransport`` adapts the NXS-P07 :class:`GovernedHttpExecutor` to the
:class:`MessagingTransport` protocol — every provider HTTP call inherits the Hub's SSRF
allow-list, bounded timeouts, TLS verification, redirect revalidation and reserved-header
stripping. No provider ever opens its own socket.
"""

from __future__ import annotations

from collections.abc import Mapping

from nexus_ai.integrations.executor import ExecutorFailure, GovernedHttpExecutor, OutboundRequest
from nexus_ai.messaging.entities import MessageChannel
from nexus_ai.messaging.errors import MessagingConfigInvalidError
from nexus_ai.messaging.providers.base import (
    MessagingProvider,
    TransportError,
    TransportResponse,
)
from nexus_ai.messaging.providers.email import EmailProvider
from nexus_ai.messaging.providers.sms import SmsProvider
from nexus_ai.messaging.providers.whatsapp import WhatsAppProvider

_PROVIDERS: dict[tuple[MessageChannel, str], MessagingProvider] = {
    (WhatsAppProvider.channel, WhatsAppProvider.provider_key): WhatsAppProvider(),
    (EmailProvider.channel, EmailProvider.provider_key): EmailProvider(),
    (SmsProvider.channel, SmsProvider.provider_key): SmsProvider(),
}

#: The provider key used when a channel is created without an explicit one.
DEFAULT_PROVIDER: dict[MessageChannel, str] = {
    MessageChannel.WHATSAPP: WhatsAppProvider.provider_key,
    MessageChannel.EMAIL: EmailProvider.provider_key,
    MessageChannel.SMS: SmsProvider.provider_key,
}


def resolve_provider(channel: MessageChannel, provider_key: str) -> MessagingProvider:
    provider = _PROVIDERS.get((channel, provider_key))
    if provider is None:
        raise MessagingConfigInvalidError(
            f"no {channel.value} provider is registered for {provider_key!r}"
        )
    return provider


def known_providers() -> tuple[tuple[str, str], ...]:
    return tuple(sorted((channel.value, key) for channel, key in _PROVIDERS))


class GovernedMessagingTransport:
    def __init__(self, executor: GovernedHttpExecutor) -> None:
        self._executor = executor

    async def request(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float | None = None,
    ) -> TransportResponse:
        try:
            raw = await self._executor.send(
                OutboundRequest(
                    method=method,
                    url=url,
                    headers=dict(headers),
                    body=body,
                    content_type=headers.get("Content-Type"),
                    timeout_seconds=timeout_seconds,
                )
            )
        except ExecutorFailure as exc:
            message = str(exc.public)
            timeout = "timed out" in message.lower()
            raise TransportError(message, timeout=timeout, connect=not timeout) from exc
        return TransportResponse(status_code=raw.status_code, headers=raw.headers, body=raw.body)
