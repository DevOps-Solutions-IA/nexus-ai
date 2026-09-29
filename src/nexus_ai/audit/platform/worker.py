"""Bounded committed-intent processing with atomic ledger and receipt."""

from uuid import UUID, uuid7

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from nexus_ai.audit.contracts import AuditConflict, AuditProvenanceError, record_digest
from nexus_ai.audit.platform.contracts import PlatformAuditIntent, semantic_digest
from nexus_ai.audit.platform.database import PlatformAuditDatabase
from nexus_ai.domain.platform_audit.models import (
    PlatformAuditHead,
    PlatformAuditIntentRecord,
    PlatformAuditReceipt,
    PlatformAuditRecord,
)


class PlatformIntentRejected(AuditProvenanceError):
    pass


class PlatformAuditWorker:
    def __init__(self, database: PlatformAuditDatabase) -> None:
        self.database = database

    async def process(
        self, source_id: UUID, *, expected: PlatformAuditIntent | None = None
    ) -> UUID:
        async with self.database.transaction() as session:
            source = await session.get(PlatformAuditIntentRecord, source_id)
            if source is None:
                raise AuditProvenanceError("committed platform source required")
            try:
                intent = PlatformAuditIntent.model_validate(source.payload)
            except ValidationError:
                raise PlatformIntentRejected("invalid platform intent") from None
            if intent.source_id != source.id:
                raise PlatformIntentRejected("platform source identity mismatch")
            semantic = semantic_digest(intent)
            if expected is not None and semantic_digest(expected) != semantic:
                raise AuditConflict("platform source semantics changed")
            head = await session.get(PlatformAuditHead, 1, with_for_update=True)
            if head is None:
                raise AuditProvenanceError("platform head missing")
            existing = await session.scalar(
                select(PlatformAuditRecord).where(PlatformAuditRecord.source_id == source_id)
            )
            if existing is not None:
                receipt = await session.get(PlatformAuditReceipt, source_id)
                if existing.semantic_digest != semantic:
                    raise AuditConflict("platform source semantics changed")
                if receipt is None or receipt.record_id != existing.id:
                    raise AuditProvenanceError("platform receipt mismatch")
                return existing.id
            identity = uuid7()
            recorded_at = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            fact = {
                "integrity_version": 1,
                "scope": "PLATFORM",
                "organization_id": None,
                "id": str(identity),
                "sequence": head.sequence + 1,
                "predecessor": head.digest,
                "recorded_at": recorded_at.isoformat(),
                "intent": intent.model_dump(mode="json"),
            }
            digest = record_digest(fact)
            session.add(
                PlatformAuditRecord(
                    id=identity,
                    source_id=source_id,
                    sequence=head.sequence + 1,
                    recorded_at=recorded_at,
                    semantic_digest=semantic,
                    predecessor=head.digest,
                    digest=digest,
                    fact=fact,
                )
            )
            await session.flush()
            session.add(PlatformAuditReceipt(source_id=source_id, record_id=identity))
            head.sequence += 1
            head.digest = digest
            return identity

    async def run_once(self) -> int:
        async with self.database.transaction() as session:
            sources = list(
                await session.scalars(
                    select(PlatformAuditIntentRecord.id)
                    .where(
                        ~select(PlatformAuditReceipt.source_id)
                        .where(PlatformAuditReceipt.source_id == PlatformAuditIntentRecord.id)
                        .exists()
                    )
                    .order_by(PlatformAuditIntentRecord.id)
                    .limit(self.database.settings.batch_size)
                )
            )
        for source_id in sources:
            try:
                await self.process(source_id)
            except PlatformIntentRejected:
                async with self.database.transaction() as session:
                    await session.execute(
                        insert(PlatformAuditReceipt)
                        .values(source_id=source_id, record_id=None, reason_code="INVALID_INTENT")
                        .on_conflict_do_nothing(index_elements=["source_id"])
                    )
        return len(sources)
