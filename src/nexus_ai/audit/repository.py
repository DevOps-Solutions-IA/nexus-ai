"""PostgreSQL serializes each tenant chain; facts never mutate or emit events."""

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy import and_, func, literal, select, text
from sqlalchemy.dialects.postgresql import insert

from nexus_ai.audit.contracts import (
    GENESIS,
    AuditConflict,
    AuditIntent,
    AuditProvenanceError,
    record_digest,
    semantic_digest,
)
from nexus_ai.domain.audit.models import AuditHead, AuditRecord
from nexus_ai.infrastructure.tenant_session import TenantSession


@dataclass(frozen=True)
class Verification:
    valid: bool
    complete: bool
    checked: int
    start: int
    end: int
    high_water: int
    anchor: str
    head_digest: str
    next_start: int | None
    error: str | None = None


class AuditRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self.tenant = tenant
        self.session = tenant.session

    async def page(
        self,
        *,
        limit: int = 50,
        after: int = 0,
        high_water: int | None = None,
        filters: dict[str, str] | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100 or not 0 <= after <= 2**63 - 1:
            raise ValueError("page exceeds bounds")
        if high_water is not None and not after <= high_water <= 2**63 - 1:
            raise ValueError("invalid snapshot cursor")
        if after and high_water is None:
            raise ValueError("continuation requires high water")
        await self.session.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        if high_water is None:
            high_water = (
                await self.session.scalar(
                    select(AuditHead.sequence).where(
                        AuditHead.organization_id == self.tenant.organization_id,
                    )
                )
                or 0
            )
        statement = select(AuditRecord).where(
            AuditRecord.organization_id == self.tenant.organization_id,
            AuditRecord.sequence > after,
            AuditRecord.sequence <= high_water,
        )
        for key, value in (filters or {}).items():
            if key == "actor_id":
                expression = AuditRecord.fact["intent"]["actor"]["user_id"].astext
            elif key in {
                "action",
                "target_type",
                "target_id",
                "outcome",
                "correlation_id",
                "source_id",
                "producer",
            }:
                expression = AuditRecord.fact["intent"][key].astext
            else:
                raise ValueError("unknown audit filter")
            statement = statement.where(expression == value)
        if since is not None:
            statement = statement.where(AuditRecord.recorded_at >= since)
        if until is not None:
            statement = statement.where(AuditRecord.recorded_at <= until)
        rows = list(
            (
                await self.session.scalars(
                    statement.order_by(AuditRecord.sequence).limit(limit + 1),
                )
            ).all()
        )
        return {
            "records": [row.fact | {"digest": row.digest} for row in rows[:limit]],
            "high_water": high_water,
            "next_after": rows[limit - 1].sequence if len(rows) > limit else None,
        }

    async def get(self, identity: uuid.UUID) -> AuditRecord | None:
        return cast(
            AuditRecord | None,
            await self.session.scalar(
                select(AuditRecord).where(
                    AuditRecord.organization_id == self.tenant.organization_id,
                    AuditRecord.id == identity,
                )
            ),
        )

    async def existing(self, intent: AuditIntent) -> AuditRecord | None:
        row = await self.session.scalar(
            select(AuditRecord).where(
                AuditRecord.organization_id == self.tenant.organization_id,
                AuditRecord.producer == intent.producer,
                AuditRecord.source_id == intent.source_id,
            )
        )
        if row is not None and row.semantic_digest != semantic_digest(intent):
            raise AuditConflict()
        return row

    async def append(self, intent: AuditIntent) -> AuditRecord:
        intent = AuditIntent.model_validate(intent.model_dump())
        if intent.organization_id != self.tenant.organization_id:
            raise AuditProvenanceError()
        await self.session.execute(
            insert(AuditHead)
            .values(
                id=self.tenant.organization_id,
                organization_id=self.tenant.organization_id,
                sequence=0,
                digest=GENESIS,
            )
            .on_conflict_do_nothing(index_elements=["organization_id"])
        )
        head = (
            await self.session.execute(
                select(AuditHead)
                .where(
                    AuditHead.organization_id == self.tenant.organization_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        existing = await self.existing(intent)
        if existing is not None:
            return existing
        original = intent.metadata.original_record_id
        if original is not None and await self.get(original) is None:
            raise AuditProvenanceError("correction must reference an existing same-tenant fact")
        identity = uuid.uuid7()
        recorded_at = await self.session.scalar(select(func.clock_timestamp()))
        if not isinstance(recorded_at, dt.datetime):
            raise AuditProvenanceError("database timestamp unavailable")
        recorded_at = recorded_at.astimezone(dt.UTC)
        sequence = head.sequence + 1
        fact = {
            "scope": "TENANT",
            "integrity_version": 1,
            "id": str(identity),
            "sequence": sequence,
            "organization_id": str(self.tenant.organization_id),
            "predecessor": head.digest,
            "recorded_at": recorded_at.isoformat(),
            "intent": intent.model_dump(mode="json"),
        }
        digest = record_digest(fact)
        row = AuditRecord(
            id=identity,
            organization_id=self.tenant.organization_id,
            sequence=sequence,
            producer=intent.producer,
            source_id=intent.source_id,
            original_record_id=original,
            recorded_at=recorded_at,
            semantic_digest=semantic_digest(intent),
            predecessor=head.digest,
            digest=digest,
            fact=fact,
        )
        self.session.add(row)
        head.sequence, head.digest = sequence, digest
        await self.session.flush()
        return row

    async def verify(self, *, start: int = 1, limit: int = 500) -> Verification:
        if not 1 <= limit <= 1000 or not 1 <= start <= 2**63 - 1001:
            raise ValueError("verification range exceeds bounds")
        await self.session.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        pairs = (
            await self.session.execute(
                select(AuditHead, AuditRecord)
                .select_from(select(literal(1)).subquery())
                .outerjoin(AuditHead, AuditHead.organization_id == self.tenant.organization_id)
                .outerjoin(
                    AuditRecord,
                    and_(
                        AuditRecord.organization_id == self.tenant.organization_id,
                        AuditRecord.sequence >= max(1, start - 1),
                        AuditRecord.sequence <= start + limit - 1,
                    ),
                )
                .order_by(AuditRecord.sequence)
                .execution_options(populate_existing=True)
            )
        ).all()
        if not pairs or (pairs[0][0] is None and pairs[0][1] is None):
            return Verification(True, start == 1, 0, start, 0, 0, GENESIS, GENESIS, None)
        head = pairs[0][0]
        if head is None:
            return Verification(
                False, False, 0, start, 0, 0, GENESIS, GENESIS, None, "HEAD_MISSING"
            )
        rows = [pair[1] for pair in pairs if pair[1] is not None]
        anchor = GENESIS
        error = None
        if start > 1:
            if rows and rows[0].sequence == start - 1:
                previous = rows.pop(0)
                anchor = previous.digest
                if not self._valid_record(previous):
                    error = "ANCHOR_INVALID"
            else:
                error = "ANCHOR_MISSING"
        expected = start
        predecessor = anchor
        checked = 0
        for row in rows:
            if row.sequence > head.sequence or row.sequence != expected:
                error = "SEQUENCE_INVALID"
                break
            if row.predecessor != predecessor or not self._valid_record(row):
                error = "CONTENT_INVALID"
                break
            predecessor = row.digest
            expected += 1
            checked += 1
        end = expected - 1
        if error is None and end < min(head.sequence, start + limit - 1):
            error = "RECORD_MISSING"
        if error is None and end == head.sequence and predecessor != head.digest:
            error = "HEAD_INVALID"
        return Verification(
            error is None,
            error is None and start == 1 and end == head.sequence,
            checked,
            start,
            end,
            head.sequence,
            anchor,
            head.digest,
            end + 1 if error is None and end < head.sequence else None,
            error,
        )

    def _valid_record(self, row: AuditRecord) -> bool:
        try:
            intent = AuditIntent.model_validate(row.fact["intent"])
            expected: dict[str, Any] = {
                "scope": "TENANT",
                "integrity_version": 1,
                "id": str(row.id),
                "sequence": row.sequence,
                "organization_id": str(row.organization_id),
                "predecessor": row.predecessor,
                "recorded_at": row.recorded_at.astimezone(dt.UTC).isoformat(),
                "intent": intent.model_dump(mode="json"),
            }
            return (
                row.organization_id == self.tenant.organization_id == intent.organization_id
                and row.producer == intent.producer
                and row.source_id == intent.source_id
                and row.original_record_id == intent.metadata.original_record_id
                and row.fact == expected
                and record_digest(expected) == row.digest
                and semantic_digest(intent) == row.semantic_digest
            )
        except ValueError, KeyError, TypeError, ValidationError:
            return False
