"""Same-transaction source journal append; no direct broker publication."""

from typing import Literal
from uuid import UUID, uuid7

from pydantic import BaseModel
from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai.audit.contracts import AuditProvenanceError
from nexus_ai.audit.platform.contracts import (
    PRODUCERS,
    PlatformAuditActor,
    PlatformAuditIntent,
    PlatformAuditMetadata,
)
from nexus_ai.domain.platform_audit.models import PlatformAuditIntentRecord


async def emit_platform_audit(
    session: AsyncSession,
    *,
    producer: str = "sentinel",
    action: str,
    target_type: str,
    target_id: UUID,
    actor: PlatformAuditActor,
    metadata: BaseModel | None = None,
    source_id: UUID | None = None,
    outcome: Literal["SUCCESS", "FAILED", "DENIED", "AMBIGUOUS"] = "SUCCESS",
) -> UUID:
    identity = (
        await session.execute(
            text(
                "SELECT current_user, session_user, "
                "coalesce(current_setting('nxs.organization_id', true), '')"
            )
        )
    ).one()
    specification = PRODUCERS.get(producer)
    if specification is None or identity != (
        specification.source_role,
        specification.source_role,
        "",
    ):
        raise AuditProvenanceError("platform source identity invalid")
    intent = PlatformAuditIntent(
        producer=producer,
        source_id=source_id or uuid7(),
        action=action,
        target_type=target_type,
        target_id=target_id,
        actor=actor,
        metadata=metadata or PlatformAuditMetadata(),
        occurred_at=await session.scalar(select(func.clock_timestamp())),
        outcome=outcome,
    )
    await session.execute(
        insert(PlatformAuditIntentRecord).values(
            id=intent.source_id, payload=intent.model_dump(mode="json")
        )
    )
    return intent.source_id
