"""Trusted source transaction hook using the P04 tenant outbox."""

import datetime as dt
import uuid

from sqlalchemy import select

from nexus_ai.audit.contracts import AuditActor, AuditConflict, AuditIntent, AuditMetadata
from nexus_ai.core.config import get_settings
from nexus_ai.core.context import current_context
from nexus_ai.domain.events.models import EventOutboxRecord
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.infrastructure.event_outbox import OutboxRepository
from nexus_ai.infrastructure.tenant_session import TenantSession


def safe_identity(value: str | None) -> uuid.UUID | None:
    if value is None or len(value) > 36:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


async def emit_audit(
    tenant: TenantSession,
    *,
    producer: str,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    actor: AuditActor,
    source_id: uuid.UUID | None = None,
    occurred_at: dt.datetime | None = None,
    outcome: str = "SUCCESS",
    metadata: AuditMetadata | None = None,
    correlation_id: uuid.UUID | None = None,
    causation_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
) -> EventEnvelope:
    context = current_context()
    if context is not None:
        correlation_id = correlation_id or safe_identity(context.correlation_id)
        request_id = request_id or safe_identity(context.request_id)
    causation_id = causation_id or source_id
    intent = AuditIntent.model_validate(
        {
            "organization_id": tenant.organization_id,
            "source_id": source_id or uuid.uuid7(),
            "producer": producer,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "actor": actor,
            "occurred_at": occurred_at or dt.datetime.now(dt.UTC),
            "outcome": outcome,
            "metadata": metadata or AuditMetadata(),
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "request_id": request_id,
        }
    )
    envelope = EventEnvelope.create(
        event_type=AuditIntent.EVENT_TYPE,
        event_version=1,
        aggregate_type="audit_intent",
        aggregate_id=str(intent.source_id),
        producer=producer,
        payload=intent.model_dump(mode="json"),
        organization_id=tenant.organization_id,
        occurred_at=intent.occurred_at,
        correlation_id=str(correlation_id or intent.source_id),
        causation_id=str(causation_id) if causation_id else None,
    )
    settings = get_settings()
    subject = str(
        envelope.subject(
            prefix=settings.events.subject_prefix,
            environment=settings.environment.value,
        )
    )
    inserted = await OutboxRepository().enqueue(tenant.session, envelope, subject)
    if not inserted:
        existing = await tenant.session.scalar(
            select(EventOutboxRecord).where(
                EventOutboxRecord.id == envelope.event_id,
            )
        )
        if existing is None or existing.envelope != envelope.model_dump(mode="json"):
            raise AuditConflict()
        return EventEnvelope.model_validate(existing.envelope)
    return envelope
