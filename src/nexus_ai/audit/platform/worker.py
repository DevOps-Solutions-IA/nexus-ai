"""Bounded committed-intent processing with atomic ledger and receipt."""

from uuid import UUID, uuid7

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from nexus_ai.audit.contracts import AuditConflict, AuditProvenanceError, record_digest
from nexus_ai.audit.platform.contracts import (
    PRODUCERS,
    PlatformAuditIntent,
    domain_head_id,
    integrity_domain,
    semantic_digest,
)
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
            source = await session.get(PlatformAuditIntentRecord, source_id, with_for_update=True)
            if source is None:
                raise AuditProvenanceError("committed platform source required")
            return await self._append(session, source, expected=expected)

    async def _append(
        self,
        session: AsyncSession,
        source: PlatformAuditIntentRecord,
        *,
        expected: PlatformAuditIntent | None = None,
        skip_busy: bool = False,
    ) -> UUID:
        try:
            intent = PlatformAuditIntent.model_validate(source.payload)
        except ValidationError:
            raise PlatformIntentRejected("invalid platform intent") from None
        if (
            intent.source_id != source.id
            or PRODUCERS[intent.producer].source_role != source.source_role
        ):
            raise PlatformIntentRejected("platform source identity mismatch")
        semantic = semantic_digest(intent)
        if expected is not None and semantic_digest(expected) != semantic:
            raise AuditConflict("platform source semantics changed")
        existing = await session.scalar(
            select(PlatformAuditRecord).where(PlatformAuditRecord.source_id == source.id)
        )
        if existing is not None:
            receipt = await session.get(PlatformAuditReceipt, source.id)
            if existing.semantic_digest != semantic:
                raise AuditConflict("platform source semantics changed")
            if receipt is None or receipt.record_id != existing.id:
                raise AuditProvenanceError("platform receipt mismatch")
            return existing.id
        if await session.get(PlatformAuditReceipt, source.id) is not None:
            raise PlatformIntentRejected("platform source already rejected")
        domain = integrity_domain(intent.producer, intent.target_id)
        head = await session.get(
            PlatformAuditHead, domain_head_id(domain), with_for_update={"nowait": skip_busy}
        )
        if head is None:
            raise AuditProvenanceError("platform head missing")
        identity = uuid7()
        recorded_at = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        fact = {
            "integrity_version": 2,
            "domain": domain,
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
                source_id=source.id,
                domain=domain,
                sequence=head.sequence + 1,
                recorded_at=recorded_at,
                semantic_digest=semantic,
                predecessor=head.digest,
                digest=digest,
                fact=fact,
            )
        )
        await session.flush()
        session.add(PlatformAuditReceipt(source_id=source.id, record_id=identity))
        head.sequence += 1
        head.digest = digest
        return identity

    async def run_once(self) -> int:
        processed = 0
        attempted: list[UUID] = []
        for _ in range(self.database.settings.batch_size):
            try:
                async with self.database.transaction() as session:
                    source = await session.scalar(
                        select(PlatformAuditIntentRecord)
                        .where(
                            PlatformAuditIntentRecord.id.not_in(attempted),
                            ~select(PlatformAuditReceipt.source_id)
                            .where(PlatformAuditReceipt.source_id == PlatformAuditIntentRecord.id)
                            .exists(),
                        )
                        .order_by(PlatformAuditIntentRecord.id)
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                    if source is None:
                        break
                    attempted.append(source.id)
                    if await session.get(PlatformAuditReceipt, source.id) is not None:
                        continue
                    try:
                        await self._append(session, source, skip_busy=True)
                    except PlatformIntentRejected:
                        session.add(
                            PlatformAuditReceipt(
                                source_id=source.id, record_id=None, reason_code="INVALID_INTENT"
                            )
                        )
                processed += 1
            except DBAPIError as error:
                if getattr(error.orig, "sqlstate", None) != "55P03":
                    raise
        return processed
