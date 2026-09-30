"""P04 durable ingestion that validates source semantics before receipt shortcuts."""

from contextlib import suppress
from typing import TYPE_CHECKING

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai.audit.contracts import AuditIntent, AuditProvenanceError
from nexus_ai.audit.repository import AuditRepository
from nexus_ai.core.config import Settings
from nexus_ai.domain.events.models import ConsumerReceiptRecord, EventOutboxRecord
from nexus_ai.events.consumer import ConsumerSpec, DurableConsumer, EventContext
from nexus_ai.events.envelope import EventEnvelope, EventScope
from nexus_ai.events.errors import EventContractError
from nexus_ai.events.idempotency import ConsumerReceiptStore, ReceiptClaim
from nexus_ai.events.service import EventPlatform
from nexus_ai.infrastructure.database import Database
from nexus_ai.infrastructure.tenant_session import TenantSession

if TYPE_CHECKING:
    from nats.aio.msg import Msg


async def verify_source(session: AsyncSession, envelope: EventEnvelope) -> AuditIntent:
    if envelope.scope is not EventScope.TENANT or envelope.event_type != AuditIntent.EVENT_TYPE:
        raise AuditProvenanceError()
    try:
        intent = AuditIntent.model_validate(envelope.payload)
    except ValidationError:
        raise AuditProvenanceError() from None
    if (
        envelope.event_version != 1
        or envelope.producer != intent.producer
        or envelope.organization_id != intent.organization_id
        or envelope.aggregate_type != "audit_intent"
        or envelope.aggregate_id != str(intent.source_id)
        or envelope.occurred_at != intent.occurred_at
    ):
        raise AuditProvenanceError()
    row = await session.scalar(
        select(EventOutboxRecord)
        .where(
            EventOutboxRecord.id == envelope.event_id,
            EventOutboxRecord.organization_id == intent.organization_id,
        )
        .with_for_update(read=True)
    )
    if (
        row is None
        or row.event_type != AuditIntent.EVENT_TYPE
        or row.event_version != 1
        or row.envelope != envelope.model_dump(mode="json")
    ):
        raise AuditProvenanceError()
    return intent


async def consume_intent(context: EventContext) -> None:
    intent = await verify_source(context.session, context.envelope)
    await AuditRepository(TenantSession(intent.organization_id, context.session)).append(intent)


class AuditReceiptStore(ConsumerReceiptStore):
    async def claim(
        self,
        session: AsyncSession,
        *,
        consumer_name: str,
        envelope: EventEnvelope,
        delivery_count: int,
    ) -> ReceiptClaim:
        intent = await verify_source(session, envelope)
        claim = await super().claim(
            session, consumer_name=consumer_name, envelope=envelope, delivery_count=delivery_count
        )
        existing = await AuditRepository(TenantSession(intent.organization_id, session)).existing(
            intent
        )
        if claim.already_finished and existing is None:
            raise AuditProvenanceError("finished receipt has no matching immutable fact")
        return claim

    async def record_terminal(
        self,
        session: AsyncSession,
        *,
        consumer_name: str,
        envelope: EventEnvelope,
        error_code: str,
        attempt: int,
    ) -> None:
        existing = await session.scalar(
            select(ConsumerReceiptRecord)
            .where(
                ConsumerReceiptRecord.consumer_name == consumer_name,
                ConsumerReceiptRecord.event_id == envelope.event_id,
            )
            .with_for_update()
        )
        if existing is not None and existing.status == "PROCESSED":
            return
        await super().record_terminal(
            session,
            consumer_name=consumer_name,
            envelope=envelope,
            error_code=error_code,
            attempt=attempt,
        )


class AuditConsumer(DurableConsumer):
    @staticmethod
    def _safe_envelope(envelope: EventEnvelope) -> EventEnvelope:
        return EventEnvelope.create(
            event_type=AuditIntent.EVENT_TYPE,
            event_version=1,
            aggregate_type="audit_intent",
            aggregate_id=str(envelope.event_id),
            producer="audit-ingestion",
            payload={},
            organization_id=envelope.organization_id,
            correlation_id=str(envelope.event_id),
            occurred_at=envelope.occurred_at,
        ).model_copy(update={"event_id": envelope.event_id})

    async def _malformed(self, message: Msg, exc: EventContractError) -> None:
        self.stats.dead_lettered += 1
        await self._log.aerror("audit_unparseable", error_code="NXS_AUDIT_ENVELOPE_INVALID")
        with suppress(Exception):
            await message.term()

    async def _retry_or_terminal(
        self,
        message: Msg,
        envelope: EventEnvelope,
        exc: BaseException,
        delivery: int,
    ) -> None:
        await super()._retry_or_terminal(
            message,
            self._safe_envelope(envelope),
            exc,
            delivery,
        )

    async def _terminal(
        self,
        message: Msg,
        envelope: EventEnvelope,
        exc: BaseException,
        *,
        delivery: int,
    ) -> None:
        if envelope.scope is not EventScope.TENANT:
            await message.term()
            self.stats.dead_lettered += 1
            return
        safe = self._safe_envelope(envelope)
        await super()._terminal(message, safe, AuditProvenanceError(), delivery=delivery)


def register_audit_consumer(
    platform: EventPlatform,
    database: Database,
    settings: Settings,
) -> AuditConsumer:
    consumer = AuditConsumer(
        ConsumerSpec(
            name="audit-ledger-v1",
            subject_filter=f"{settings.events.subject_prefix}.{settings.environment.value}.tenant.audit.>",
            handler=consume_intent,
            event_types=frozenset({AuditIntent.EVENT_TYPE}),
        ),
        database=database,
        transport=platform.transport,
        dead_letters=platform.dead_letters,
        receipts=AuditReceiptStore(),
        settings=settings.events,
    )
    platform.consumers.add(consumer)
    return consumer
