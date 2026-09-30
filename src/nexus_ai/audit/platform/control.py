"""Bounded platform reads require live authentication and an explicit global grant."""

from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select

from nexus_ai.audit.contracts import GENESIS, AuditProvenanceError, record_digest
from nexus_ai.audit.platform.contracts import (
    LEGACY_DOMAIN,
    PlatformAuditIntent,
    domain_head_id,
    integrity_domain,
    semantic_digest,
)
from nexus_ai.audit.platform.database import PlatformAuditDatabase
from nexus_ai.domain.auth.models import UserRecord
from nexus_ai.domain.auth.state import PrincipalStateValidator
from nexus_ai.domain.auth.tokens import TokenService
from nexus_ai.domain.platform_audit.models import PlatformAuditHead, PlatformAuditRecord
from nexus_ai.domain.provisioning.models import PlatformGrantRecord
from nexus_ai.infrastructure.database import Database


class PlatformAuditPermission(StrEnum):
    READ = "audit:platform:read"
    VERIFY = "audit:platform:verify"


class PlatformAuditAuthority:
    def __init__(
        self,
        tokens: TokenService,
        validator: PrincipalStateValidator,
        authorization_database: Database,
    ) -> None:
        self.tokens = tokens
        self.validator = validator
        self.database = authorization_database

    async def require(self, token: str, permission: PlatformAuditPermission) -> UUID:
        if not isinstance(permission, PlatformAuditPermission) or not 1 <= len(token) <= 8192:
            raise AuditProvenanceError("platform authentication denied")
        claims = self.tokens.verify_access_token(token)
        await self.validator.require_valid(
            user_id=claims.subject,
            session_id=claims.session_id,
            organization_id=claims.organization_id,
        )
        async with self.database.transaction() as session:
            identity = await session.scalar(
                select(PlatformGrantRecord.user_id)
                .join(UserRecord, UserRecord.id == PlatformGrantRecord.user_id)
                .where(
                    PlatformGrantRecord.user_id == claims.subject,
                    PlatformGrantRecord.capability == permission.value,
                    UserRecord.status == "ACTIVE",
                )
            )
        if identity is None:
            raise AuditProvenanceError("explicit platform audit grant required")
        return identity


class PlatformAuditControl:
    def __init__(self, authority: PlatformAuditAuthority, database: PlatformAuditDatabase) -> None:
        self.authority = authority
        self.database = database

    async def domains(self, token: str) -> list[dict[str, Any]]:
        await self.authority.require(token, PlatformAuditPermission.READ)
        async with self.database.transaction(snapshot=True) as session:
            heads = list(
                await session.scalars(select(PlatformAuditHead).order_by(PlatformAuditHead.id))
            )
            if [head.id for head in heads] != list(range(1, 18)):
                raise AuditProvenanceError("platform domains missing")
            return [
                {
                    "domain": LEGACY_DOMAIN if head.id == 1 else f"platform:v2:{head.id - 2:02x}",
                    "high_water": head.sequence,
                    "digest": head.digest,
                }
                for head in heads
            ]

    async def verify_domains(
        self,
        token: str,
        *,
        domains: list[str],
        limit: int = 200,
    ) -> dict[str, Any]:
        self.bounds(0, limit, None)
        if not 1 <= len(domains) <= 17 or len(set(domains)) != len(domains):
            raise ValueError("invalid platform domains")
        head_ids = [domain_head_id(domain) for domain in domains]
        await self.authority.require(token, PlatformAuditPermission.VERIFY)
        results = []
        async with self.database.transaction(snapshot=True) as session:
            for domain, head_id in zip(domains, head_ids, strict=True):
                head = await session.get(PlatformAuditHead, head_id)
                if head is None:
                    raise AuditProvenanceError("platform head missing")
                rows = list(
                    await session.scalars(
                        select(PlatformAuditRecord)
                        .where(PlatformAuditRecord.domain == domain)
                        .order_by(PlatformAuditRecord.sequence)
                        .limit(limit)
                    )
                )
                sequence, digest = 0, GENESIS
                for row in rows:
                    self.validate_record(row, sequence + 1, digest)
                    sequence, digest = row.sequence, row.digest
                if sequence != min(head.sequence, limit) or (
                    sequence == head.sequence and digest != head.digest
                ):
                    raise AuditProvenanceError("platform chain truncated")
                results.append(
                    {
                        "domain": domain,
                        "through": sequence,
                        "high_water": head.sequence,
                        "digest": digest,
                        "complete": sequence == head.sequence,
                    }
                )
        return {
            "domains": results,
            "complete": all(result["complete"] for result in results),
            "all_domains": len(domains) == 17,
        }

    @staticmethod
    def validate_record(row: PlatformAuditRecord, sequence: int, predecessor: str) -> None:
        try:
            intent = PlatformAuditIntent.model_validate(row.fact["intent"])
        except KeyError, ValidationError:
            raise AuditProvenanceError("platform fact invalid") from None
        expected_fact = {
            "integrity_version": 1 if row.domain == LEGACY_DOMAIN else 2,
            "scope": "PLATFORM",
            "organization_id": None,
            "id": str(row.id),
            "sequence": sequence,
            "predecessor": predecessor,
            "recorded_at": row.recorded_at.isoformat(),
            "intent": intent.model_dump(mode="json"),
        }
        if row.domain != LEGACY_DOMAIN:
            if row.domain != integrity_domain(intent.producer, intent.target_id):
                raise AuditProvenanceError("platform integrity domain mismatch")
            expected_fact["domain"] = row.domain
        if (
            row.sequence != sequence
            or row.predecessor != predecessor
            or row.source_id != intent.source_id
            or row.fact != expected_fact
            or row.semantic_digest != semantic_digest(intent)
            or row.digest != record_digest(expected_fact)
        ):
            raise AuditProvenanceError("platform integrity mismatch")

    @staticmethod
    def bounds(after: int, limit: int, high_water: int | None) -> None:
        if (
            type(after) is not int
            or type(limit) is not int
            or not 0 <= after <= 9_223_372_036_854_775_807
            or (after > 0 and high_water is None)
            or not 1 <= limit <= 200
            or (
                high_water is not None
                and (
                    type(high_water) is not int
                    or not after <= high_water <= 9_223_372_036_854_775_807
                )
            )
        ):
            raise ValueError("invalid platform audit page")

    async def records(
        self,
        token: str,
        *,
        domain: str,
        after: int = 0,
        limit: int = 50,
        high_water: int | None = None,
    ) -> dict[str, Any]:
        self.bounds(after, limit, high_water)
        await self.authority.require(token, PlatformAuditPermission.READ)
        async with self.database.transaction(snapshot=True) as session:
            head = await session.get(PlatformAuditHead, domain_head_id(domain))
            if head is None:
                raise AuditProvenanceError("platform head missing")
            cutoff = head.sequence if high_water is None else high_water
            if cutoff > head.sequence:
                raise ValueError("invalid platform snapshot")
            records = list(
                await session.scalars(
                    select(PlatformAuditRecord)
                    .where(
                        PlatformAuditRecord.domain == domain,
                        PlatformAuditRecord.sequence > after,
                        PlatformAuditRecord.sequence <= cutoff,
                    )
                    .order_by(PlatformAuditRecord.sequence)
                    .limit(limit)
                )
            )
            return {
                "domain": domain,
                "records": [{"fact": row.fact, "digest": row.digest} for row in records],
                "high_water": cutoff,
                "next_after": records[-1].sequence if records else after,
            }

    async def verify(
        self,
        token: str,
        *,
        domain: str,
        after: int = 0,
        limit: int = 200,
        high_water: int | None = None,
    ) -> dict[str, Any]:
        self.bounds(after, limit, high_water)
        await self.authority.require(token, PlatformAuditPermission.VERIFY)
        async with self.database.transaction(snapshot=True) as session:
            head = await session.get(PlatformAuditHead, domain_head_id(domain))
            if head is None:
                raise AuditProvenanceError("platform head missing")
            cutoff = head.sequence if high_water is None else high_water
            if cutoff > head.sequence or after > cutoff:
                raise ValueError("invalid platform snapshot")
            anchor = GENESIS
            if after:
                predecessor = await session.scalar(
                    select(PlatformAuditRecord).where(
                        PlatformAuditRecord.domain == domain, PlatformAuditRecord.sequence == after
                    )
                )
                if predecessor is None:
                    raise AuditProvenanceError("platform anchor invalid")
                self.validate_record(predecessor, after, predecessor.predecessor)
                anchor = predecessor.digest
            rows = list(
                await session.scalars(
                    select(PlatformAuditRecord)
                    .where(
                        PlatformAuditRecord.domain == domain,
                        PlatformAuditRecord.sequence > after,
                        PlatformAuditRecord.sequence <= cutoff,
                    )
                    .order_by(PlatformAuditRecord.sequence)
                    .limit(limit)
                )
            )
            sequence = after
            predecessor_digest = anchor
            for row in rows:
                self.validate_record(row, sequence + 1, predecessor_digest)
                sequence = row.sequence
                predecessor_digest = row.digest
            expected_end = min(cutoff, after + limit)
            if sequence != expected_end or (
                sequence == head.sequence and predecessor_digest != head.digest
            ):
                raise AuditProvenanceError("platform chain truncated")
            return {
                "domain": domain,
                "scope": "PLATFORM",
                "after": after,
                "through": sequence,
                "high_water": cutoff,
                "anchor": anchor,
                "digest": predecessor_digest,
                "complete": after == 0 and sequence == cutoff,
            }
