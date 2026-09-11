"""AI Agent Runtime persistence (NXS-P13).

Every repository is tenant-scoped: constructed with a ``TenantSession`` so every query is
RLS-confined whatever the WHERE clause says, and the composite tenant-aware FKs into the
NXS-P06 / NXS-P11 / NXS-P12 tables refuse a cross-tenant attachment at the database.
Session and turn transitions take a row lock (``SELECT ... FOR UPDATE``).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select, update

from nexus_ai.agents.entities import (
    AgentChannel,
    AgentDefinition,
    AgentSession,
    AgentStatus,
    AgentToolCall,
    AgentToolCallStatus,
    AgentTurn,
    ModelProfile,
    ModelProfileStatus,
    ModelProvider,
    ModelProviderAccount,
    ModelProviderAccountStatus,
)
from nexus_ai.agents.state_machine import (
    AgentSessionDisposition,
    AgentSessionState,
    AgentTurnState,
)
from nexus_ai.domain.agents.models import (
    AiAgentRecord,
    AiAgentSessionRecord,
    AiAgentToolCallRecord,
    AiAgentTurnRecord,
    AiModelProfileRecord,
    AiModelProviderAccountRecord,
    AiModelSecretRecord,
    AiModelUsageRecord,
)
from nexus_ai.infrastructure.tenant_session import TenantSession
from nexus_ai.integrations.credentials import CredentialType, EncryptedSecret


def _to_account(row: AiModelProviderAccountRecord) -> ModelProviderAccount:
    return ModelProviderAccount(
        id=row.id,
        organization_id=row.organization_id,
        provider=ModelProvider(row.provider),
        slug=row.slug,
        api_base=row.api_base,
        external_account_id=row.external_account_id,
        credential_ref=row.credential_ref,
        status=ModelProviderAccountStatus(row.status),
        configuration=dict(row.configuration),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_profile(row: AiModelProfileRecord) -> ModelProfile:
    return ModelProfile(
        id=row.id,
        organization_id=row.organization_id,
        account_id=row.account_id,
        slug=row.slug,
        display_name=row.display_name,
        model=row.model,
        temperature=row.temperature,
        max_output_tokens=row.max_output_tokens,
        status=ModelProfileStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_agent(row: AiAgentRecord) -> AgentDefinition:
    return AgentDefinition(
        id=row.id,
        organization_id=row.organization_id,
        slug=row.slug,
        display_name=row.display_name,
        status=AgentStatus(row.status),
        model_profile_id=row.model_profile_id,
        system_instructions=row.system_instructions,
        tool_keys=tuple(row.tool_keys),
        max_tool_iterations=row.max_tool_iterations,
        max_output_tokens=row.max_output_tokens,
        temperature=row.temperature,
        timeout_seconds=row.timeout_seconds,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_session(row: AiAgentSessionRecord) -> AgentSession:
    return AgentSession(
        id=row.id,
        organization_id=row.organization_id,
        agent_id=row.agent_id,
        model_profile_id=row.model_profile_id,
        state=AgentSessionState(row.state),
        disposition=AgentSessionDisposition(row.disposition) if row.disposition else None,
        channel=AgentChannel(row.channel),
        initiator_user_id=row.initiator_user_id,
        initiator_session_id=row.initiator_session_id,
        conversation_id=row.conversation_id,
        customer_id=row.customer_id,
        call_id=row.call_id,
        voice_session_id=row.voice_session_id,
        correlation_id=row.correlation_id,
        idempotency_key=row.idempotency_key,
        request_fingerprint=row.request_fingerprint,
        turn_count=row.turn_count,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        tool_call_count=row.tool_call_count,
        error_code=row.error_code,
        started_at=row.started_at,
        last_activity_at=row.last_activity_at,
        ended_at=row.ended_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_turn(row: AiAgentTurnRecord) -> AgentTurn:
    return AgentTurn(
        id=row.id,
        organization_id=row.organization_id,
        session_id=row.session_id,
        sequence=row.sequence,
        state=AgentTurnState(row.state),
        channel=AgentChannel(row.channel),
        input_text=row.input_text,
        response_text=row.response_text,
        input_char_count=row.input_char_count,
        response_char_count=row.response_char_count,
        model=row.model,
        finish_reason=row.finish_reason,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        tool_iterations=row.tool_iterations,
        tool_call_count=row.tool_call_count,
        latency_ms=row.latency_ms,
        error_code=row.error_code,
        idempotency_key=row.idempotency_key,
        request_fingerprint=row.request_fingerprint,
        response_correlation_id=row.response_correlation_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_tool_call(row: AiAgentToolCallRecord) -> AgentToolCall:
    return AgentToolCall(
        id=row.id,
        organization_id=row.organization_id,
        session_id=row.session_id,
        turn_id=row.turn_id,
        sequence=row.sequence,
        iteration=row.iteration,
        tool_key=row.tool_key,
        arguments_hash=row.arguments_hash,
        status=AgentToolCallStatus(row.status),
        tool_result_class=row.tool_result_class,
        tool_status_code=row.tool_status_code,
        denied_reason=row.denied_reason,
        latency_ms=row.latency_ms,
        created_at=row.created_at,
    )


class _Base:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session
        self._org = tenant.organization_id


class ModelProviderAccountRepository(_Base):
    async def by_id(self, account_id: uuid.UUID) -> ModelProviderAccount | None:
        row = await self._session.get(AiModelProviderAccountRecord, account_id)
        if row is None or row.organization_id != self._org:
            return None
        return _to_account(row)

    async def by_slug(self, slug: str) -> ModelProviderAccount | None:
        row = (
            await self._session.execute(
                select(AiModelProviderAccountRecord).where(
                    AiModelProviderAccountRecord.organization_id == self._org,
                    AiModelProviderAccountRecord.slug == slug,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _to_account(row)

    async def list_all(self, *, limit: int) -> list[ModelProviderAccount]:
        rows = (
            (
                await self._session.execute(
                    select(AiModelProviderAccountRecord)
                    .where(AiModelProviderAccountRecord.organization_id == self._org)
                    .order_by(AiModelProviderAccountRecord.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_to_account(row) for row in rows]

    async def insert(
        self,
        *,
        provider: ModelProvider,
        slug: str,
        api_base: str,
        external_account_id: str,
        configuration: dict[str, Any],
    ) -> ModelProviderAccount:
        now = dt.datetime.now(dt.UTC)
        record = AiModelProviderAccountRecord(
            id=uuid.uuid7(),
            organization_id=self._org,
            provider=provider.value,
            slug=slug,
            api_base=api_base,
            external_account_id=external_account_id,
            credential_ref=None,
            status=ModelProviderAccountStatus.ACTIVE.value,
            configuration=configuration,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)
        return _to_account(record)

    async def apply(
        self, account_id: uuid.UUID, changes: dict[str, Any]
    ) -> ModelProviderAccount | None:
        row = (
            await self._session.execute(
                update(AiModelProviderAccountRecord)
                .where(
                    AiModelProviderAccountRecord.organization_id == self._org,
                    AiModelProviderAccountRecord.id == account_id,
                )
                .values(**changes, updated_at=dt.datetime.now(dt.UTC))
                .returning(AiModelProviderAccountRecord)
            )
        ).scalar_one_or_none()
        return None if row is None else _to_account(row)


class ModelProfileRepository(_Base):
    async def by_id(self, profile_id: uuid.UUID) -> ModelProfile | None:
        row = await self._session.get(AiModelProfileRecord, profile_id)
        if row is None or row.organization_id != self._org:
            return None
        return _to_profile(row)

    async def list_all(self, *, limit: int) -> list[ModelProfile]:
        rows = (
            (
                await self._session.execute(
                    select(AiModelProfileRecord)
                    .where(AiModelProfileRecord.organization_id == self._org)
                    .order_by(AiModelProfileRecord.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_to_profile(row) for row in rows]

    async def insert(
        self,
        *,
        account_id: uuid.UUID,
        slug: str,
        display_name: str,
        model: str,
        temperature: float | None,
        max_output_tokens: int | None,
    ) -> ModelProfile:
        now = dt.datetime.now(dt.UTC)
        record = AiModelProfileRecord(
            id=uuid.uuid7(),
            organization_id=self._org,
            account_id=account_id,
            slug=slug,
            display_name=display_name,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            status=ModelProfileStatus.ACTIVE.value,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)
        return _to_profile(record)

    async def apply(self, profile_id: uuid.UUID, changes: dict[str, Any]) -> ModelProfile | None:
        row = (
            await self._session.execute(
                update(AiModelProfileRecord)
                .where(
                    AiModelProfileRecord.organization_id == self._org,
                    AiModelProfileRecord.id == profile_id,
                )
                .values(**changes, updated_at=dt.datetime.now(dt.UTC))
                .returning(AiModelProfileRecord)
            )
        ).scalar_one_or_none()
        return None if row is None else _to_profile(row)


class AgentRepository(_Base):
    async def by_id(self, agent_id: uuid.UUID) -> AgentDefinition | None:
        row = await self._session.get(AiAgentRecord, agent_id)
        if row is None or row.organization_id != self._org:
            return None
        return _to_agent(row)

    async def list_all(self, *, limit: int) -> list[AgentDefinition]:
        rows = (
            (
                await self._session.execute(
                    select(AiAgentRecord)
                    .where(AiAgentRecord.organization_id == self._org)
                    .order_by(AiAgentRecord.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_to_agent(row) for row in rows]

    async def insert(
        self,
        *,
        slug: str,
        display_name: str,
        model_profile_id: uuid.UUID,
        system_instructions: str,
        tool_keys: tuple[str, ...],
        max_tool_iterations: int,
        max_output_tokens: int | None,
        temperature: float | None,
        timeout_seconds: float | None,
    ) -> AgentDefinition:
        now = dt.datetime.now(dt.UTC)
        record = AiAgentRecord(
            id=uuid.uuid7(),
            organization_id=self._org,
            slug=slug,
            display_name=display_name,
            status=AgentStatus.ACTIVE.value,
            model_profile_id=model_profile_id,
            system_instructions=system_instructions,
            tool_keys=list(tool_keys),
            max_tool_iterations=max_tool_iterations,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            timeout_seconds=timeout_seconds,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)
        return _to_agent(record)

    async def apply(self, agent_id: uuid.UUID, changes: dict[str, Any]) -> AgentDefinition | None:
        if "tool_keys" in changes and isinstance(changes["tool_keys"], tuple):
            changes["tool_keys"] = list(changes["tool_keys"])
        row = (
            await self._session.execute(
                update(AiAgentRecord)
                .where(
                    AiAgentRecord.organization_id == self._org,
                    AiAgentRecord.id == agent_id,
                )
                .values(**changes, updated_at=dt.datetime.now(dt.UTC))
                .returning(AiAgentRecord)
            )
        ).scalar_one_or_none()
        return None if row is None else _to_agent(row)


class AgentSessionRepository(_Base):
    async def by_id(
        self, session_id: uuid.UUID, *, for_update: bool = False
    ) -> AgentSession | None:
        stmt = select(AiAgentSessionRecord).where(
            AiAgentSessionRecord.organization_id == self._org,
            AiAgentSessionRecord.id == session_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return None if row is None else _to_session(row)

    async def by_idempotency_key(self, key: str) -> AgentSession | None:
        row = (
            await self._session.execute(
                select(AiAgentSessionRecord).where(
                    AiAgentSessionRecord.organization_id == self._org,
                    AiAgentSessionRecord.idempotency_key == key,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _to_session(row)

    async def list_all(self, *, agent_id: uuid.UUID | None, limit: int) -> list[AgentSession]:
        stmt = select(AiAgentSessionRecord).where(AiAgentSessionRecord.organization_id == self._org)
        if agent_id is not None:
            stmt = stmt.where(AiAgentSessionRecord.agent_id == agent_id)
        rows = (
            (
                await self._session.execute(
                    stmt.order_by(AiAgentSessionRecord.created_at.desc()).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_to_session(row) for row in rows]

    async def insert(self, record_values: dict[str, Any]) -> AgentSession:
        now = dt.datetime.now(dt.UTC)
        record = AiAgentSessionRecord(
            id=uuid.uuid7(),
            organization_id=self._org,
            created_at=now,
            updated_at=now,
            **record_values,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)
        return _to_session(record)

    async def apply(self, session_id: uuid.UUID, changes: dict[str, Any]) -> AgentSession | None:
        row = (
            await self._session.execute(
                update(AiAgentSessionRecord)
                .where(
                    AiAgentSessionRecord.organization_id == self._org,
                    AiAgentSessionRecord.id == session_id,
                )
                .values(**changes, updated_at=dt.datetime.now(dt.UTC))
                .returning(AiAgentSessionRecord)
            )
        ).scalar_one_or_none()
        return None if row is None else _to_session(row)


class AgentTurnRepository(_Base):
    async def by_id(self, turn_id: uuid.UUID) -> AgentTurn | None:
        row = await self._session.get(AiAgentTurnRecord, turn_id)
        if row is None or row.organization_id != self._org:
            return None
        return _to_turn(row)

    async def by_session_idempotency_key(self, session_id: uuid.UUID, key: str) -> AgentTurn | None:
        row = (
            await self._session.execute(
                select(AiAgentTurnRecord).where(
                    AiAgentTurnRecord.organization_id == self._org,
                    AiAgentTurnRecord.session_id == session_id,
                    AiAgentTurnRecord.idempotency_key == key,
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _to_turn(row)

    async def active_for_session(self, session_id: uuid.UUID) -> AgentTurn | None:
        row = (
            await self._session.execute(
                select(AiAgentTurnRecord).where(
                    AiAgentTurnRecord.organization_id == self._org,
                    AiAgentTurnRecord.session_id == session_id,
                    AiAgentTurnRecord.state.in_(
                        [
                            s.value
                            for s in AgentTurnState
                            if s.value not in ("COMPLETED", "FAILED", "CANCELLED")
                        ]
                    ),
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _to_turn(row)

    async def list_for_session(self, session_id: uuid.UUID, *, limit: int) -> list[AgentTurn]:
        rows = (
            (
                await self._session.execute(
                    select(AiAgentTurnRecord)
                    .where(
                        AiAgentTurnRecord.organization_id == self._org,
                        AiAgentTurnRecord.session_id == session_id,
                    )
                    .order_by(AiAgentTurnRecord.sequence.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_to_turn(row) for row in rows]

    async def insert(self, record_values: dict[str, Any]) -> AgentTurn:
        now = dt.datetime.now(dt.UTC)
        record = AiAgentTurnRecord(
            id=uuid.uuid7(),
            organization_id=self._org,
            created_at=now,
            updated_at=now,
            **record_values,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)
        return _to_turn(record)

    async def apply(self, turn_id: uuid.UUID, changes: dict[str, Any]) -> AgentTurn | None:
        row = (
            await self._session.execute(
                update(AiAgentTurnRecord)
                .where(
                    AiAgentTurnRecord.organization_id == self._org,
                    AiAgentTurnRecord.id == turn_id,
                )
                .values(**changes, updated_at=dt.datetime.now(dt.UTC))
                .returning(AiAgentTurnRecord)
            )
        ).scalar_one_or_none()
        return None if row is None else _to_turn(row)


class AgentToolCallRepository(_Base):
    async def insert(self, record_values: dict[str, Any]) -> AgentToolCall:
        record = AiAgentToolCallRecord(
            id=uuid.uuid7(),
            organization_id=self._org,
            created_at=dt.datetime.now(dt.UTC),
            **record_values,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)
        return _to_tool_call(record)

    async def list_for_turn(self, turn_id: uuid.UUID) -> list[AgentToolCall]:
        rows = (
            (
                await self._session.execute(
                    select(AiAgentToolCallRecord)
                    .where(
                        AiAgentToolCallRecord.organization_id == self._org,
                        AiAgentToolCallRecord.turn_id == turn_id,
                    )
                    .order_by(AiAgentToolCallRecord.sequence.asc())
                )
            )
            .scalars()
            .all()
        )
        return [_to_tool_call(row) for row in rows]


class ModelUsageRepository(_Base):
    async def upsert(self, session_id: uuid.UUID, values: dict[str, Any]) -> None:
        existing = (
            await self._session.execute(
                select(AiModelUsageRecord).where(
                    AiModelUsageRecord.organization_id == self._org,
                    AiModelUsageRecord.session_id == session_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            self._session.add(
                AiModelUsageRecord(
                    id=uuid.uuid7(),
                    organization_id=self._org,
                    session_id=session_id,
                    recorded_at=dt.datetime.now(dt.UTC),
                    **values,
                )
            )
        else:
            await self._session.execute(
                update(AiModelUsageRecord)
                .where(
                    AiModelUsageRecord.organization_id == self._org,
                    AiModelUsageRecord.session_id == session_id,
                )
                .values(**values, recorded_at=dt.datetime.now(dt.UTC))
            )
        await self._session.flush()


class ModelSecretStore:
    """Adapts ``ai_model_secrets`` to the NXS-P07 encrypted-secret store contract."""

    def __init__(self, database: Any) -> None:
        self._db = database

    async def get(self, organization_id: uuid.UUID, ref: str) -> EncryptedSecret | None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            row = (
                await tenant.session.execute(
                    select(AiModelSecretRecord).where(
                        AiModelSecretRecord.organization_id == organization_id,
                        AiModelSecretRecord.credential_ref == ref,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return EncryptedSecret(
                credential_type=CredentialType(row.credential_type), ciphertext=row.ciphertext
            )

    async def put(self, organization_id: uuid.UUID, ref: str, secret: EncryptedSecret) -> None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            existing = (
                await tenant.session.execute(
                    select(AiModelSecretRecord).where(
                        AiModelSecretRecord.organization_id == organization_id,
                        AiModelSecretRecord.credential_ref == ref,
                    )
                )
            ).scalar_one_or_none()
            now = dt.datetime.now(dt.UTC)
            if existing is None:
                tenant.session.add(
                    AiModelSecretRecord(
                        id=uuid.uuid7(),
                        organization_id=organization_id,
                        credential_ref=ref,
                        credential_type=secret.credential_type.value,
                        ciphertext=secret.ciphertext,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                existing.ciphertext = secret.ciphertext
                existing.credential_type = secret.credential_type.value
                existing.updated_at = now

    async def delete(self, organization_id: uuid.UUID, ref: str) -> bool:
        from sqlalchemy import delete as _delete

        async with self._db.tenant_transaction(organization_id) as tenant:
            result = await tenant.session.execute(
                _delete(AiModelSecretRecord).where(
                    AiModelSecretRecord.organization_id == organization_id,
                    AiModelSecretRecord.credential_ref == ref,
                )
            )
            return bool(result.rowcount)
