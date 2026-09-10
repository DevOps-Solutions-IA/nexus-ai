"""Inbound voice provider callbacks (NXS-P12, ADR-0089).

ElevenLabs sends a signed post-call webhook. The flow mirrors NXS-P11:

    provider callback -> webhook token -> account resolution -> signature + freshness
    verification -> provider normalization -> durable per-event idempotency claim ->
    session usage reconciliation -> P04 outbox event -> ACK

A security-invalid callback is NEVER ACKed as accepted. The trusted ``organization_id``
comes from the webhook token, NEVER from the payload. The claim + the reconciliation +
the outbox event commit ATOMICALLY in one tenant transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from nexus_ai.core.config import Settings
from nexus_ai.core.logging import get_logger
from nexus_ai.domain.voice.repository import (
    VoiceProviderAccountRepository,
    VoiceProviderEventRepository,
    VoiceSessionRepository,
    VoiceUsageRepository,
)
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.infrastructure.database import Database
from nexus_ai.integrations.credentials import SecretMaterial, VaultClient
from nexus_ai.integrations.webhooks import decode_webhook_token
from nexus_ai.voice.entities import VoiceAccountStatus, VoiceLatencyMetrics, VoiceProviderAccount
from nexus_ai.voice.errors import VoiceNotAuthorizedError, VoiceWebhookInvalidError
from nexus_ai.voice.providers.base import WebhookContext
from nexus_ai.voice.providers.registry import resolve_voice_provider


@dataclass(frozen=True, slots=True)
class VoiceWebhookAcceptance:
    accepted: bool
    processed: str


class InboundVoiceService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        publisher: EventPublisher,
        vault: VaultClient,
    ) -> None:
        self._settings = settings
        self._db = database
        self._publisher = publisher
        self._vault = vault
        self._log = get_logger("nexus_ai.voice.inbound")

    async def receive(
        self, provider_key: str, token: str, ctx: WebhookContext
    ) -> VoiceWebhookAcceptance:
        if len(ctx.body) > self._settings.voice.max_webhook_body_bytes:
            raise VoiceWebhookInvalidError("the callback body exceeds the size limit")
        organization_id, account = await self._resolve_account(provider_key, token)
        if account.status is not VoiceAccountStatus.ACTIVE:
            raise VoiceNotAuthorizedError("the voice provider account is disabled")

        adapter = resolve_voice_provider(account.provider)
        secret = await self._resolve_secret(organization_id, account)
        adapter.verify_webhook(
            ctx,
            secret,
            tolerance_seconds=self._settings.voice.webhook_timestamp_tolerance_seconds,
        )
        parsed = adapter.parse_webhook(ctx)
        if parsed.provider_event_id is None:
            return VoiceWebhookAcceptance(accepted=True, processed="ignored:no-event-id")

        try:
            async with self._db.tenant_transaction(organization_id) as tenant:
                events = VoiceProviderEventRepository(tenant)
                try:
                    await events.record(
                        account_id=account.id,
                        session_id=None,
                        provider_event_id=parsed.provider_event_id,
                        kind="USAGE",
                        outcome="CLAIMED",
                        detail={"kind": "post_call"},
                    )
                except IntegrityError:
                    return VoiceWebhookAcceptance(accepted=True, processed="replayed")

                sessions = VoiceSessionRepository(tenant)
                session = await sessions.by_provider_session_id(
                    account.id, parsed.provider_event_id, for_update=True
                )
                if session is not None and parsed.usage_characters is not None:
                    usage = session.usage
                    if usage is not None:
                        usage = usage.model_copy(
                            update={"provider_characters": parsed.usage_characters}
                        )
                        await sessions.apply(session.id, {"usage": usage.model_dump(mode="json")})
                        latency = session.latency or VoiceLatencyMetrics()
                        if parsed.duration_ms is not None:
                            latency = latency.model_copy(
                                update={"session_duration_ms": parsed.duration_ms}
                            )
                        await VoiceUsageRepository(tenant).upsert(
                            session_id=session.id, usage=usage, latency=latency
                        )
                await self._publisher.enqueue(
                    tenant.session,
                    EventEnvelope.create(
                        event_type="voice.usage.recorded",
                        event_version=1,
                        aggregate_type="voice_session",
                        aggregate_id=str(session.id) if session is not None else account.id.hex,
                        producer=self._settings.service_name,
                        organization_id=organization_id,
                        payload={
                            "session_id": str(session.id)
                            if session is not None
                            else str(UUID(int=0)),
                            "account_id": str(account.id),
                            "audio_seconds_in": 0.0,
                            "audio_seconds_out": 0.0,
                            "provider_characters": parsed.usage_characters or 0,
                            "interruptions": 0,
                            "session_duration_ms": parsed.duration_ms,
                        },
                    ),
                )
        except IntegrityError:
            return VoiceWebhookAcceptance(accepted=True, processed="replayed")
        return VoiceWebhookAcceptance(accepted=True, processed="reconciled")

    async def _resolve_account(
        self, provider_key: str, token: str
    ) -> tuple[UUID, VoiceProviderAccount]:
        from nexus_ai.integrations.errors import WebhookEndpointNotFoundError

        try:
            organization_id = decode_webhook_token(token)
        except WebhookEndpointNotFoundError as exc:
            raise VoiceWebhookInvalidError("the webhook token is invalid") from exc
        async with self._db.tenant_transaction(organization_id) as tenant:
            account = await VoiceProviderAccountRepository(tenant).by_webhook_token(token)
        if account is None or account.provider.value != provider_key:
            raise VoiceWebhookInvalidError("the webhook token does not match an account")
        return organization_id, account

    async def _resolve_secret(
        self, organization_id: UUID, account: VoiceProviderAccount
    ) -> SecretMaterial | None:
        if account.credential_ref is None:
            return None
        return await self._vault.get_secret(organization_id, account.credential_ref)
