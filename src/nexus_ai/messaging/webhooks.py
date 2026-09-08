"""Inbound messaging webhook pipeline (NXS-P09).

Deterministic order (a security-invalid webhook is NEVER ACKed as accepted, and no
Customer or Conversation is created before authenticity passes):

  1  identify the provider from the route
  2  decode the tenant scope from the unguessable token (NEVER from the body)
  3  resolve the provider account (RLS-scoped) — an unknown account fails closed
  4  size-bound the body
  5  GET challenge / provider verification (signature, verify token) — raises on failure
  6  reject a disabled account (after verification, so an attacker learns nothing new)
  7  normalize the provider payload into shared domain objects
  8  per event: durable dedupe / replay claim
  9  NXS-P06 customer identity resolution (inbound sender -> Customer)
  10 NXS-P06 conversation / thread resolution
  11 persist the normalized inbound message + timeline + P04 event atomically with the claim
  12 fold delivery-status callbacks into the canonical message state
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from nexus_ai.core.config import Settings
from nexus_ai.core.context import current_context
from nexus_ai.core.logging import get_logger
from nexus_ai.domain.customers.entities import ActivityType
from nexus_ai.domain.customers.repository import ConversationActivityRepository
from nexus_ai.domain.customers.service import ConversationService, CustomerService
from nexus_ai.domain.messaging.models import MessagingInboundReceiptRecord
from nexus_ai.domain.messaging.repository import (
    MessagingAccountRepository,
    MessagingMessageRepository,
)
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.infrastructure.database import Database
from nexus_ai.integrations.credentials import SecretMaterial, VaultClient
from nexus_ai.integrations.errors import WebhookEndpointNotFoundError
from nexus_ai.integrations.webhooks import decode_webhook_token
from nexus_ai.messaging.addresses import normalize_address
from nexus_ai.messaging.entities import (
    AccountStatus,
    Message,
    MessageDirection,
    MessageStatus,
    MessagingAccount,
)
from nexus_ai.messaging.errors import (
    MessagingAccountDisabledError,
    MessagingAuthFailedError,
    MessagingPayloadInvalidError,
)
from nexus_ai.messaging.providers.base import (
    NormalizedInbound,
    WebhookContext,
)
from nexus_ai.messaging.providers.registry import resolve_provider
from nexus_ai.messaging.service import (
    MessagingService,
    resolve_inbound_conversation,
    resolve_inbound_customer,
)


@dataclass(frozen=True, slots=True)
class InboundResult:
    accepted: bool
    challenge: str | None = None
    received: int = 0
    status_updates: int = 0
    replayed: int = 0
    message_ids: list[str] = field(default_factory=list)


class InboundMessagingService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        publisher: EventPublisher,
        vault: VaultClient,
        customers: CustomerService,
        conversations: ConversationService,
        outbound: MessagingService,
    ) -> None:
        self._settings = settings
        self._db = database
        self._publisher = publisher
        self._vault = vault
        self._customers = customers
        self._conversations = conversations
        self._outbound = outbound
        self._log = get_logger("nexus_ai.messaging.webhooks")

    async def receive(self, provider_route: str, token: str, ctx: WebhookContext) -> InboundResult:
        try:
            organization_id = decode_webhook_token(token)
        except WebhookEndpointNotFoundError as exc:
            raise MessagingAuthFailedError("the webhook token is malformed") from exc

        async with self._db.tenant_transaction(organization_id) as tenant:
            account = await MessagingAccountRepository(tenant).by_webhook_token(token)
        if account is None or account.provider != provider_route:
            raise MessagingAuthFailedError("no messaging account matches this webhook route")

        if len(ctx.body) > self._settings.channels.max_webhook_body_bytes:
            raise MessagingPayloadInvalidError("the webhook body exceeds the configured limit")

        provider = resolve_provider(account.channel, account.provider)
        secret = await self._resolve_secret(organization_id, account)

        if ctx.method.upper() == "GET":
            await provider.verify_webhook(account, ctx, secret)
            return InboundResult(accepted=True, challenge=provider.webhook_challenge(account, ctx))

        await provider.verify_webhook(account, ctx, secret)
        if account.status is not AccountStatus.ACTIVE:
            raise MessagingAccountDisabledError("the messaging account is disabled")

        parsed = provider.parse_webhook(account, ctx)
        result = InboundResult(accepted=True, message_ids=[])
        received = replayed = status_updates = 0

        for inbound in parsed.inbound:
            event_id = f"msg:{inbound.provider_message_id}"
            stored = await self._process_inbound(organization_id, account, inbound, event_id)
            if stored is None:
                replayed += 1
            else:
                received += 1
                result.message_ids.append(str(stored.id))

        for status in parsed.statuses:
            event_id = f"st:{status.provider_message_id}:{status.provider_status}"
            claimed = await self._claim_only(organization_id, account, event_id)
            if not claimed:
                replayed += 1
                continue
            advanced = await self._outbound.apply_delivery_status(
                organization_id,
                account,
                status.provider_message_id,
                status.status,
                provider_status=status.provider_status,
                occurred_at=status.occurred_at,
                error_code=status.error_code,
                provider_code=status.provider_code,
            )
            if advanced:
                status_updates += 1

        return InboundResult(
            accepted=True,
            received=received,
            status_updates=status_updates,
            replayed=replayed,
            message_ids=result.message_ids,
        )

    # -- inbound message processing --------------------------------------------

    async def _process_inbound(
        self,
        organization_id: UUID,
        account: MessagingAccount,
        inbound: NormalizedInbound,
        event_id: str,
    ) -> Message | None:
        default_country = account.configuration.get("default_country")
        sender = normalize_address(
            account.channel, inbound.sender_raw, default_country=default_country
        )
        # NXS-P06 owns identity + conversation; these calls are duplicate-safe.
        customer_id = await resolve_inbound_customer(
            self._customers, organization_id, account.channel, sender.value, None
        )
        conversation = await resolve_inbound_conversation(
            self._conversations,
            organization_id,
            account,
            customer_id,
            inbound.external_thread_hint,
        )

        message = Message(
            id=uuid.uuid7(),
            organization_id=organization_id,
            conversation_id=conversation.id,
            customer_id=customer_id,
            channel=account.channel,
            direction=MessageDirection.INBOUND,
            status=MessageStatus.RECEIVED,
            provider=account.provider,
            provider_account_id=account.id,
            provider_message_id=inbound.provider_message_id[:200],
            sender=sender,
            recipients=(
                normalize_address(
                    account.channel, inbound.recipient_raw, default_country=default_country
                ),
            ),
            content=inbound.content,
            email=inbound.email,
            sms=None,
            reply_to_message_id=None,
            correlation_id=self._correlation(),
            idempotency_key=None,
            error_code=None,
            provider_timestamp=inbound.provider_timestamp,
            sent_at=None,
            delivered_at=None,
            read_at=None,
            failed_at=None,
            created_at=_now(),
            updated_at=_now(),
        )

        try:
            async with self._db.tenant_transaction(organization_id) as tenant:
                # The receipt claim commits ATOMICALLY with the message so a crash mid
                # processing rolls both back and a provider retry reprocesses cleanly.
                tenant.session.add(
                    MessagingInboundReceiptRecord(
                        id=uuid.uuid7(),
                        organization_id=organization_id,
                        account_id=account.id,
                        event_id=event_id[:400],
                        message_id=message.id,
                        status="ACCEPTED",
                        created_at=_now(),
                    )
                )
                await tenant.session.flush()
                stored = await MessagingMessageRepository(tenant).insert(message)
                await ConversationActivityRepository(tenant).append(
                    customer_id=stored.customer_id,
                    conversation_id=stored.conversation_id,
                    activity_type=ActivityType.MESSAGE_INBOUND,
                    dedup_key=f"messaging.inbound:{stored.id}",
                    data={"channel": stored.channel.value, "message_id": str(stored.id)},
                )
                await self._enqueue_received(tenant.session, organization_id, stored)
        except IntegrityError:
            # A concurrent duplicate webhook won the receipt claim — replay, no new row.
            return None
        return stored

    async def _claim_only(
        self, organization_id: UUID, account: MessagingAccount, event_id: str
    ) -> bool:
        try:
            async with self._db.tenant_transaction(organization_id) as tenant:
                tenant.session.add(
                    MessagingInboundReceiptRecord(
                        id=uuid.uuid7(),
                        organization_id=organization_id,
                        account_id=account.id,
                        event_id=event_id[:400],
                        message_id=None,
                        status="ACCEPTED",
                        created_at=_now(),
                    )
                )
                await tenant.session.flush()
                return True
        except IntegrityError:
            return False

    async def _resolve_secret(
        self, organization_id: UUID, account: MessagingAccount
    ) -> SecretMaterial | None:
        if account.credential_ref is None:
            return None
        return await self._vault.get_secret(organization_id, account.credential_ref)

    async def _enqueue_received(
        self, session: Any, organization_id: UUID, message: Message
    ) -> None:
        ctx = current_context()
        envelope = EventEnvelope.create(
            event_type="messaging.message.received",
            event_version=1,
            aggregate_type="messaging_message",
            aggregate_id=str(message.id),
            producer=self._settings.service_name,
            organization_id=organization_id,
            correlation_id=None if ctx is None else ctx.correlation_id,
            payload={
                "message_id": str(message.id),
                "conversation_id": str(message.conversation_id),
                "channel": message.channel.value,
                "direction": message.direction.value,
                "provider": message.provider,
                "provider_message_id": message.provider_message_id,
                "customer_id": (None if message.customer_id is None else str(message.customer_id)),
                "replayed": False,
            },
        )
        await self._publisher.enqueue(session, envelope)

    @staticmethod
    def _correlation() -> str | None:
        ctx = current_context()
        return None if ctx is None else ctx.correlation_id


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


__all__ = ["InboundMessagingService", "InboundResult"]
