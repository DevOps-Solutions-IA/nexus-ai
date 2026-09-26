"""Internal operator facade: existing token/live state plus explicit platform grants."""

from enum import StrEnum
from uuid import UUID, uuid7

from sqlalchemy import select

from nexus_ai.domain.auth.models import UserRecord
from nexus_ai.domain.auth.state import PrincipalStateValidator
from nexus_ai.domain.auth.tokens import TokenService
from nexus_ai.domain.provisioning.models import PlatformGrantRecord
from nexus_ai.infrastructure.database import Database
from nexus_ai.sentinel.contracts import Approval, IncidentState
from nexus_ai.sentinel.errors import SentinelDenied
from nexus_ai.sentinel.execution import SentinelActionExecutor
from nexus_ai.sentinel.service import SentinelStore


class SentinelPermission(StrEnum):
    READ = "sentinel:read"
    TRIAGE = "sentinel:triage"
    APPROVE = "sentinel:approve"
    CONTROL = "sentinel:control"


class SentinelOperatorAuthority:
    def __init__(
        self,
        tokens: TokenService,
        validator: PrincipalStateValidator,
        authorization_database: Database,
    ) -> None:
        self._tokens = tokens
        self._validator = validator
        self._database = authorization_database

    async def require(self, token: str, permission: SentinelPermission) -> UUID:
        if not isinstance(permission, SentinelPermission) or not 1 <= len(token) <= 8192:
            raise SentinelDenied("operator_authentication_denied")
        claims = self._tokens.verify_access_token(token)
        await self._validator.require_valid(
            user_id=claims.subject,
            session_id=claims.session_id,
            organization_id=claims.organization_id,
        )
        async with self._database.transaction() as session:
            identity = await session.scalar(
                select(PlatformGrantRecord.user_id)
                .join(
                    UserRecord,
                    UserRecord.id == PlatformGrantRecord.user_id,
                )
                .where(
                    PlatformGrantRecord.user_id == claims.subject,
                    PlatformGrantRecord.capability == permission.value,
                    UserRecord.status == "ACTIVE",
                )
            )
        if identity is None:
            raise SentinelDenied("sentinel_platform_grant_required")
        return identity


class SentinelControl:
    def __init__(
        self,
        authority: SentinelOperatorAuthority,
        store: SentinelStore,
        executor: SentinelActionExecutor,
    ) -> None:
        self._authority = authority
        self._store = store
        self._executor = executor

    async def incidents(
        self, token: str, *, after: UUID | None = None, limit: int = 50
    ) -> list[UUID]:
        await self._authority.require(token, SentinelPermission.READ)
        return await self._store.list_incidents(after=after, limit=limit)

    async def approve(self, token: str, approval: Approval) -> UUID:
        actor = await self._authority.require(token, SentinelPermission.APPROVE)
        request = Approval.model_validate({**approval.model_dump(), "approver_principal": actor})
        return await self._store.record_approval(request)

    async def set_mutable_actions(self, token: str, revision: int, *, enabled: bool) -> int:
        await self._authority.require(token, SentinelPermission.CONTROL)
        return await self._store.set_mutable_actions(revision, enabled=enabled)

    async def transition(
        self, token: str, incident_id: UUID, revision: int, state: IncidentState
    ) -> int:
        await self._authority.require(token, SentinelPermission.TRIAGE)
        return await self._store.transition(
            incident_id,
            revision,
            state,
            resolution_source="AUTHORIZED_OPERATOR" if state == IncidentState.RESOLVED else None,
        )

    async def execute(self, token: str, proposal_id: UUID) -> str:
        await self._authority.require(token, SentinelPermission.CONTROL)
        claim = await self._executor.claim(proposal_id, uuid7())
        await self._authority.require(token, SentinelPermission.CONTROL)
        return await self._executor.dispatch(claim)
