"""The AI Agent Runtime application service (NXS-P13: NXS-AGENT-001, ADR-0090/0094).

Owns: model provider account / model profile / agent CRUD; agent-session lifecycle;
turn submission and the bounded model/tool loop; cancellation; usage accounting; and safe
P04 event publication. It does NOT own telephony, SIP, ElevenLabs transport, direct
integrations, credentials-at-rest, durable workflows, campaigns, the scheduler or
human-agent operations. Every external action goes through the NXS-P08 Tool Engine; every
model REST call goes through the NXS-P07 governed transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import uuid
from collections.abc import Callable
from typing import Any, cast
from uuid import UUID

from nexus_ai.agents.context import AgentContextBuilder, PriorTurn
from nexus_ai.agents.entities import (
    AgentDefinition,
    AgentResponse,
    AgentSession,
    AgentStatus,
    AgentToolCallStatus,
    AgentTurn,
    CreateAgentRequest,
    CreateModelProfileRequest,
    ModelProfile,
    ModelProfileStatus,
    ModelProvider,
    ModelProviderAccount,
    ModelProviderAccountStatus,
    RegisterModelProviderAccountRequest,
    StartAgentSessionRequest,
    StopAgentSessionRequest,
    StoreModelCredentialRequest,
    SubmitTurnRequest,
    UpdateAgentRequest,
    UpdateModelProfileRequest,
    UpdateModelProviderAccountRequest,
)
from nexus_ai.agents.errors import (
    AgentBusyError,
    AgentConfigInvalidError,
    AgentDisabledError,
    AgentInvalidStateError,
    AgentModelProfileNotFoundError,
    AgentModelProviderAccountNotFoundError,
    AgentNotFoundError,
    AgentSessionExpiredError,
    AgentSessionNotFoundError,
    AgentTurnNotFoundError,
    AgentTurnTimeoutError,
)
from nexus_ai.agents.events import SESSION_STATE_EVENT_TYPE
from nexus_ai.agents.idempotency import start_session_fingerprint, turn_fingerprint
from nexus_ai.agents.models.base import ModelProviderAdapter, ModelToolSpec
from nexus_ai.agents.models.registry import resolve_model_provider
from nexus_ai.agents.runtime import AgentRuntime, TurnContext, TurnOutcome
from nexus_ai.agents.state_machine import (
    AgentSessionState,
    AgentTurnState,
    FoldOutcome,
    fold_agent_session_state,
    session_is_terminal,
    session_rank,
)
from nexus_ai.agents.toolbridge import AgentToolBridge, ToolCallOutcome
from nexus_ai.core.config import Settings
from nexus_ai.core.errors import NxsError
from nexus_ai.core.logging import get_logger
from nexus_ai.domain.agents.repository import (
    AgentRepository,
    AgentSessionRepository,
    AgentToolCallRepository,
    AgentTurnRepository,
    ModelProfileRepository,
    ModelProviderAccountRepository,
    ModelUsageRepository,
)
from nexus_ai.domain.auth.entities import Principal
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.infrastructure.database import Database
from nexus_ai.infrastructure.tenant_session import TenantSession
from nexus_ai.integrations.credentials import CredentialType, SecretMaterial, VaultClient
from nexus_ai.integrations.destination import DestinationPolicy
from nexus_ai.integrations.errors import IntegrationDestinationBlockedError
from nexus_ai.tools.registry import ToolRegistry
from nexus_ai.tools.service import ToolEngine

_LIVE_RANK = 1

ModelProviderFactory = Callable[[ModelProvider], ModelProviderAdapter]


class AgentService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        publisher: EventPublisher,
        vault: VaultClient,
        tool_engine: ToolEngine,
        tool_registry: ToolRegistry,
        model_http_transport: Any,
        *,
        destination_policy: DestinationPolicy | None = None,
        model_provider_factory: ModelProviderFactory | None = None,
    ) -> None:
        self._settings = settings
        self._cfg = settings.agents
        self._db = database
        self._publisher = publisher
        self._vault = vault
        self._tools = tool_engine
        self._tool_registry = tool_registry
        self._http = model_http_transport
        self._destinations = destination_policy or DestinationPolicy()
        self._provider_factory = model_provider_factory or resolve_model_provider
        self._bridge = AgentToolBridge(tool_engine, self._cfg)
        self._runtime = AgentRuntime(self._cfg, self._bridge)
        self._context = AgentContextBuilder(self._cfg)
        self._turn_tasks: dict[UUID, asyncio.Task[TurnOutcome]] = {}
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._log = get_logger("nexus_ai.agents.service")

    # -- model provider accounts --------------------------------------------------

    async def create_account(
        self, organization_id: UUID, request: RegisterModelProviderAccountRequest
    ) -> ModelProviderAccount:
        self._require_enabled()
        try:
            validated = self._destinations.validate_url(request.api_base)
            if not validated.is_https:
                raise AgentConfigInvalidError("the model API base must be an https origin")
        except IntegrationDestinationBlockedError as exc:
            reason = exc.extensions.get("reason", "blocked")
            raise AgentConfigInvalidError(
                f"the model API base is not an allowed destination ({reason})"
            ) from exc
        async with self._db.tenant_transaction(organization_id) as tenant:
            if await ModelProviderAccountRepository(tenant).by_slug(request.slug) is not None:
                raise AgentConfigInvalidError("a model provider account with this slug exists")
            return await ModelProviderAccountRepository(tenant).insert(
                provider=request.provider,
                slug=request.slug,
                api_base=request.api_base,
                external_account_id=request.external_account_id,
                configuration=dict(request.configuration),
            )

    async def get_account(self, organization_id: UUID, account_id: UUID) -> ModelProviderAccount:
        async with self._db.tenant_transaction(organization_id) as tenant:
            account = await ModelProviderAccountRepository(tenant).by_id(account_id)
        if account is None:
            raise AgentModelProviderAccountNotFoundError("no such model provider account")
        return account

    async def list_accounts(
        self, organization_id: UUID, *, limit: int
    ) -> list[ModelProviderAccount]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await ModelProviderAccountRepository(tenant).list_all(limit=limit)

    async def update_account(
        self, organization_id: UUID, account_id: UUID, request: UpdateModelProviderAccountRequest
    ) -> ModelProviderAccount:
        changes: dict[str, Any] = {}
        if request.api_base is not None:
            try:
                if not self._destinations.validate_url(request.api_base).is_https:
                    raise AgentConfigInvalidError("the model API base must be an https origin")
            except IntegrationDestinationBlockedError as exc:
                reason = exc.extensions.get("reason", "blocked")
                raise AgentConfigInvalidError(
                    f"the model API base is not an allowed destination ({reason})"
                ) from exc
            changes["api_base"] = request.api_base
        if request.configuration is not None:
            changes["configuration"] = dict(request.configuration)
        if request.status is not None:
            changes["status"] = request.status.value
        async with self._db.tenant_transaction(organization_id) as tenant:
            updated = await ModelProviderAccountRepository(tenant).apply(account_id, changes)
        if updated is None:
            raise AgentModelProviderAccountNotFoundError("no such model provider account")
        return updated

    async def store_account_credential(
        self, organization_id: UUID, account_id: UUID, request: StoreModelCredentialRequest
    ) -> None:
        account = await self.get_account(organization_id, account_id)
        ref = account.credential_ref or f"ai-model:{account.id}"
        material = SecretMaterial(CredentialType.PROVIDER_SECRET_SET, dict(request.fields))
        await self._vault.store_secret(organization_id, ref, material)
        async with self._db.tenant_transaction(organization_id) as tenant:
            await ModelProviderAccountRepository(tenant).apply(account_id, {"credential_ref": ref})

    # -- model profiles ---------------------------------------------------------

    async def create_profile(
        self, organization_id: UUID, request: CreateModelProfileRequest
    ) -> ModelProfile:
        self._require_enabled()
        async with self._db.tenant_transaction(organization_id) as tenant:
            account = await ModelProviderAccountRepository(tenant).by_id(request.account_id)
            if account is None:
                raise AgentModelProviderAccountNotFoundError("no such model provider account")
            return await ModelProfileRepository(tenant).insert(
                account_id=request.account_id,
                slug=request.slug,
                display_name=request.display_name,
                model=request.model,
                temperature=request.temperature,
                max_output_tokens=request.max_output_tokens,
            )

    async def get_profile(self, organization_id: UUID, profile_id: UUID) -> ModelProfile:
        async with self._db.tenant_transaction(organization_id) as tenant:
            profile = await ModelProfileRepository(tenant).by_id(profile_id)
        if profile is None:
            raise AgentModelProfileNotFoundError("no such model profile")
        return profile

    async def list_profiles(self, organization_id: UUID, *, limit: int) -> list[ModelProfile]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await ModelProfileRepository(tenant).list_all(limit=limit)

    async def update_profile(
        self, organization_id: UUID, profile_id: UUID, request: UpdateModelProfileRequest
    ) -> ModelProfile:
        changes = {
            key: (value.value if hasattr(value, "value") else value)
            for key, value in request.model_dump(exclude_none=True).items()
        }
        async with self._db.tenant_transaction(organization_id) as tenant:
            updated = await ModelProfileRepository(tenant).apply(profile_id, changes)
        if updated is None:
            raise AgentModelProfileNotFoundError("no such model profile")
        return updated

    # -- agents ---------------------------------------------------------------

    async def create_agent(
        self, organization_id: UUID, request: CreateAgentRequest
    ) -> AgentDefinition:
        self._require_enabled()
        async with self._db.tenant_transaction(organization_id) as tenant:
            profile = await ModelProfileRepository(tenant).by_id(request.model_profile_id)
            if profile is None:
                raise AgentModelProfileNotFoundError("no such model profile")
            await self._validate_tool_keys(tenant, organization_id, request.tool_keys)
            return await AgentRepository(tenant).insert(
                slug=request.slug,
                display_name=request.display_name,
                model_profile_id=request.model_profile_id,
                system_instructions=request.system_instructions,
                tool_keys=request.tool_keys,
                max_tool_iterations=(
                    request.max_tool_iterations
                    if request.max_tool_iterations is not None
                    else self._cfg.max_tool_iterations
                ),
                max_output_tokens=request.max_output_tokens,
                temperature=request.temperature,
                timeout_seconds=request.timeout_seconds,
            )

    async def get_agent(self, organization_id: UUID, agent_id: UUID) -> AgentDefinition:
        async with self._db.tenant_transaction(organization_id) as tenant:
            agent = await AgentRepository(tenant).by_id(agent_id)
        if agent is None:
            raise AgentNotFoundError("no such agent")
        return agent

    async def list_agents(self, organization_id: UUID, *, limit: int) -> list[AgentDefinition]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await AgentRepository(tenant).list_all(limit=limit)

    async def update_agent(
        self, organization_id: UUID, agent_id: UUID, request: UpdateAgentRequest
    ) -> AgentDefinition:
        payload = request.model_dump(exclude_none=True)
        changes: dict[str, Any] = {}
        async with self._db.tenant_transaction(organization_id) as tenant:
            if "model_profile_id" in payload:
                if await ModelProfileRepository(tenant).by_id(payload["model_profile_id"]) is None:
                    raise AgentModelProfileNotFoundError("no such model profile")
                changes["model_profile_id"] = payload["model_profile_id"]
            if "tool_keys" in payload:
                await self._validate_tool_keys(tenant, organization_id, tuple(payload["tool_keys"]))
                changes["tool_keys"] = list(payload["tool_keys"])
            for key in (
                "display_name",
                "system_instructions",
                "max_tool_iterations",
                "max_output_tokens",
                "temperature",
                "timeout_seconds",
            ):
                if key in payload:
                    changes[key] = payload[key]
            if "status" in payload:
                changes["status"] = request.status.value if request.status else None
            updated = await AgentRepository(tenant).apply(agent_id, changes)
        if updated is None:
            raise AgentNotFoundError("no such agent")
        return updated

    async def _validate_tool_keys(
        self, tenant: Any, organization_id: UUID, tool_keys: tuple[str, ...]
    ) -> None:
        for key in tool_keys:
            try:
                await self._tool_registry.get_by_key(organization_id, key)
            except NxsError as exc:
                raise AgentConfigInvalidError(
                    f"tool {key!r} is not a registered tool in this Organization"
                ) from exc

    # -- sessions -----------------------------------------------------------

    async def start_session(
        self, organization_id: UUID, principal: Principal, request: StartAgentSessionRequest
    ) -> AgentSession:
        self._require_enabled()
        agent = await self.get_agent(organization_id, request.agent_id)
        if agent.status is not AgentStatus.ACTIVE:
            raise AgentConfigInvalidError("the agent is disabled")
        profile = await self.get_profile(organization_id, agent.model_profile_id)
        if profile.status is not ModelProfileStatus.ACTIVE:
            raise AgentConfigInvalidError("the agent's model profile is disabled")
        account = await self.get_account(organization_id, profile.account_id)
        if account.status is not ModelProviderAccountStatus.ACTIVE:
            raise AgentConfigInvalidError("the model provider account is disabled")

        fingerprint = start_session_fingerprint(
            agent_id=request.agent_id,
            channel=request.channel.value,
            conversation_id=request.conversation_id,
            customer_id=request.customer_id,
            call_id=request.call_id,
            voice_session_id=request.voice_session_id,
        )
        if request.idempotency_key is not None:
            replay = await self._replay_session(
                organization_id, request.idempotency_key, fingerprint
            )
            if replay is not None:
                return replay

        now = dt.datetime.now(dt.UTC)
        try:
            async with self._db.tenant_transaction(organization_id) as tenant:
                await self._assert_references(tenant, request)
                session = await AgentSessionRepository(tenant).insert(
                    {
                        "agent_id": agent.id,
                        "model_profile_id": profile.id,
                        "state": AgentSessionState.ACTIVE.value,
                        "state_rank": session_rank(AgentSessionState.ACTIVE),
                        "disposition": None,
                        "channel": request.channel.value,
                        "initiator_user_id": principal.user_id,
                        "initiator_session_id": principal.session_id,
                        "conversation_id": request.conversation_id,
                        "customer_id": request.customer_id,
                        "call_id": request.call_id,
                        "voice_session_id": request.voice_session_id,
                        "correlation_id": request.correlation_id,
                        "idempotency_key": request.idempotency_key,
                        "request_fingerprint": fingerprint,
                        "started_at": now,
                        "last_activity_at": now,
                    }
                )
                await self._emit_session_event(
                    tenant.session, organization_id, session, "agent.session.created"
                )
                await self._emit_session_event(
                    tenant.session, organization_id, session, "agent.session.started"
                )
        except NxsError:
            raise
        except Exception as exc:  # IntegrityError on the idempotency key race
            replay = await self._replay_session(
                organization_id, request.idempotency_key or "", fingerprint
            )
            if replay is not None:
                return replay
            raise AgentConfigInvalidError("the session could not be created") from exc
        return session

    async def get_session(self, organization_id: UUID, session_id: UUID) -> AgentSession:
        async with self._db.tenant_transaction(organization_id) as tenant:
            session = await AgentSessionRepository(tenant).by_id(session_id)
        if session is None:
            raise AgentSessionNotFoundError("no such agent session in this Organization")
        return session

    async def list_sessions(
        self, organization_id: UUID, *, agent_id: UUID | None, limit: int
    ) -> list[AgentSession]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await AgentSessionRepository(tenant).list_all(agent_id=agent_id, limit=limit)

    async def stop_session(
        self, organization_id: UUID, session_id: UUID, request: StopAgentSessionRequest
    ) -> AgentSession:
        del request
        return await self._terminalize(
            organization_id, session_id, AgentSessionState.COMPLETED, None
        )

    async def cancel_session(self, organization_id: UUID, session_id: UUID) -> AgentSession:
        return await self._terminalize(
            organization_id, session_id, AgentSessionState.CANCELLED, "NXS_AGENT_CANCELLED"
        )

    async def _terminalize(
        self,
        organization_id: UUID,
        session_id: UUID,
        target: AgentSessionState,
        error_code: str | None,
    ) -> AgentSession:
        # Cancellation is first-class: preempt an in-flight turn BEFORE contending for
        # the per-session lock (submit_turn holds that lock for the whole turn). The
        # cancelled turn task unwinds through submit_turn -> _fail_turn, which releases
        # the lock; only then do we take it for the terminal state transition.
        task = self._turn_tasks.get(session_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock, self._db.tenant_transaction(organization_id) as tenant:
            repo = AgentSessionRepository(tenant)
            session = await repo.by_id(session_id, for_update=True)
            if session is None:
                raise AgentSessionNotFoundError("no such agent session in this Organization")
            if session_is_terminal(session.state):
                return session
            fold = fold_agent_session_state(current=session.state, proposed=target)
            updated = await repo.apply(
                session_id,
                {
                    "state": fold.state.value,
                    "state_rank": session_rank(fold.state),
                    "disposition": (fold.disposition.value if fold.disposition else None),
                    "error_code": error_code,
                    "ended_at": dt.datetime.now(dt.UTC),
                },
            )
            assert updated is not None  # noqa: S101
            await self._emit_session_event(
                tenant.session,
                organization_id,
                updated,
                SESSION_STATE_EVENT_TYPE[fold.state.value],
            )
            await self._record_usage(tenant.session, organization_id, updated)
        return updated

    # -- turns ------------------------------------------------------------

    async def submit_turn(
        self, organization_id: UUID, session_id: UUID, request: SubmitTurnRequest
    ) -> AgentResponse:
        self._require_enabled()
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise AgentBusyError("a turn is already in flight for this session")
        async with lock:
            fingerprint = turn_fingerprint(session_id=session_id, content=request.content)
            turn, session = await self._open_turn(organization_id, session_id, request, fingerprint)
            if turn.state in (AgentTurnState.COMPLETED,):
                return self._response_from_turn(turn)

            agent = await self.get_agent(organization_id, session.agent_id)
            # The whole-turn deadline is bounded by the global ceiling, the per-Agent
            # timeout AND the session's remaining absolute lifetime — max_session_seconds
            # is an ABSOLUTE ceiling, not merely a new-turn admission check, so a turn
            # opened just before expiry cannot run models / tools past it.
            remaining = self._remaining_session_lifetime(session, dt.datetime.now(dt.UTC))
            deadline = min(self._effective_turn_deadline(agent), max(remaining, 0.0))

            task: asyncio.Task[TurnOutcome] = asyncio.create_task(
                self._run_turn(organization_id, session, turn, request, agent)
            )
            self._turn_tasks[session_id] = task
            try:
                async with asyncio.timeout(deadline):
                    outcome = await task
            except TimeoutError:
                # A deadline elapsed — cancel + drain the task so no stale model response
                # is committed and no further model / Tool Engine call can begin, then
                # raise the stable taxonomy error (never a bare TimeoutError).
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                if self._lifetime_exceeded(session, dt.datetime.now(dt.UTC)):
                    # the ABSOLUTE session lifetime expired during the turn — terminalise
                    # the session EXPIRED and surface the canonical session-expired
                    # semantics, not a misleading turn / provider timeout.
                    await self._expire_session_now(organization_id, session_id)
                    await self._fail_turn(
                        organization_id,
                        session_id,
                        turn.id,
                        "NXS_AGENT_SESSION_EXPIRED",
                        cancelled=True,
                    )
                    raise AgentSessionExpiredError(
                        "the agent session reached its lifetime ceiling during the turn"
                    ) from None
                await self._fail_turn(
                    organization_id, session_id, turn.id, "NXS_AGENT_TURN_TIMEOUT"
                )
                raise AgentTurnTimeoutError(
                    "the agent turn exceeded its execution deadline"
                ) from None
            except asyncio.CancelledError:
                await self._fail_turn(
                    organization_id, session_id, turn.id, "NXS_AGENT_CANCELLED", cancelled=True
                )
                raise
            except NxsError as exc:
                await self._fail_turn(organization_id, session_id, turn.id, exc.code)
                raise
            finally:
                self._turn_tasks.pop(session_id, None)

            return await self._finish_turn(organization_id, session, turn, request, outcome)

    async def list_turns(
        self, organization_id: UUID, session_id: UUID, *, limit: int
    ) -> list[AgentTurn]:
        await self.get_session(organization_id, session_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await AgentTurnRepository(tenant).list_for_session(session_id, limit=limit)

    async def get_turn(self, organization_id: UUID, session_id: UUID, turn_id: UUID) -> AgentTurn:
        async with self._db.tenant_transaction(organization_id) as tenant:
            turn = await AgentTurnRepository(tenant).by_id(turn_id)
        if turn is None or turn.session_id != session_id:
            raise AgentTurnNotFoundError("no such turn for this session")
        return turn

    async def _open_turn(
        self,
        organization_id: UUID,
        session_id: UUID,
        request: SubmitTurnRequest,
        fingerprint: str,
    ) -> tuple[AgentTurn, AgentSession]:
        expired = False
        async with self._db.tenant_transaction(organization_id) as tenant:
            session_repo = AgentSessionRepository(tenant)
            session = await session_repo.by_id(session_id, for_update=True)
            if session is None:
                raise AgentSessionNotFoundError("no such agent session in this Organization")
            if session_is_terminal(session.state):
                raise AgentInvalidStateError("the agent session has already ended")
            # Absolute lifetime ceiling — enforced BEFORE any model or Tool Engine call,
            # under the FOR UPDATE session-row lock so a concurrent submit cannot slip a
            # late turn through. Terminalise EXPIRED in THIS transaction (so it commits),
            # then raise outside the block.
            if self._lifetime_exceeded(session, dt.datetime.now(dt.UTC)):
                await self._terminalize_expired(tenant.session, organization_id, session)
                expired = True
            else:
                turn_repo = AgentTurnRepository(tenant)
                if request.idempotency_key is not None:
                    existing = await turn_repo.by_session_idempotency_key(
                        session_id, request.idempotency_key
                    )
                    if existing is not None:
                        if existing.request_fingerprint != fingerprint:
                            from nexus_ai.agents.errors import AgentIdempotencyConflictError

                            raise AgentIdempotencyConflictError(
                                "the idempotency key was used for a different turn"
                            )
                        return existing, session
                if await turn_repo.active_for_session(session_id) is not None:
                    raise AgentBusyError("a turn is already in flight for this session")
                sequence = session.turn_count + 1
                turn = await turn_repo.insert(
                    {
                        "session_id": session_id,
                        "sequence": sequence,
                        "state": AgentTurnState.RUNNING.value,
                        "channel": session.channel.value,
                        "input_text": request.content,
                        "response_text": None,
                        "input_char_count": len(request.content),
                        "idempotency_key": request.idempotency_key,
                        "request_fingerprint": fingerprint,
                    }
                )
                await session_repo.apply(
                    session_id,
                    {
                        "turn_count": sequence,
                        "last_activity_at": dt.datetime.now(dt.UTC),
                    },
                )
                await self._emit_turn_event(
                    tenant.session,
                    organization_id,
                    session,
                    turn,
                    "agent.turn.started",
                    {"input_char_count": len(request.content)},
                )
        if expired:
            raise AgentSessionExpiredError("the agent session reached its lifetime ceiling")
        return turn, session

    def _effective_turn_deadline(self, agent: AgentDefinition) -> float:
        """The per-turn execution deadline (model calls + tool loop + continuation).

        ``agent.timeout_seconds`` is the per-Agent maximum total turn deadline. A tenant
        value may only TIGHTEN the global ``turn_deadline_seconds`` safety ceiling, never
        widen it, so the effective deadline is the minimum of the two."""
        ceiling = self._cfg.turn_deadline_seconds
        if agent.timeout_seconds is None:
            return ceiling
        return min(agent.timeout_seconds, ceiling)

    def _lifetime_exceeded(self, session: AgentSession, now: dt.datetime) -> bool:
        """``True`` once ``started_at + max_session_seconds`` has passed — the absolute
        session lifetime ceiling. A session with no ``started_at`` (never happens after
        ``start_session``) is treated as not expired."""
        return self._remaining_session_lifetime(session, now) <= 0.0

    def _remaining_session_lifetime(self, session: AgentSession, now: dt.datetime) -> float:
        """Seconds left before ``started_at + max_session_seconds`` — the absolute
        lifetime ceiling. ``<= 0`` means the session has expired. No ``started_at`` (never
        after ``start_session``) yields the full budget."""
        started = session.started_at
        if started is None:
            return float(self._cfg.max_session_seconds)
        deadline = started + dt.timedelta(seconds=self._cfg.max_session_seconds)
        return (deadline - now).total_seconds()

    async def _expire_session_now(self, organization_id: UUID, session_id: UUID) -> None:
        """Terminalise the session EXPIRED from OUTSIDE the per-session process lock (the
        caller holds it). Its own ``SELECT … FOR UPDATE`` transaction so the terminal
        wins at the DATABASE boundary across independent workers; a no-op if the row is
        already terminal."""
        async with self._db.tenant_transaction(organization_id) as tenant:
            repo = AgentSessionRepository(tenant)
            current = await repo.by_id(session_id, for_update=True)
            if current is None or session_is_terminal(current.state):
                return
            await self._terminalize_expired(tenant.session, organization_id, current)

    async def _terminalize_expired(
        self, session_conn: Any, organization_id: UUID, session: AgentSession
    ) -> None:
        """Persist the EXPIRED terminal + ``ended_at`` and emit ``agent.session.expired``,
        within the caller's already-locked transaction. The canonical fold guards it: a
        session that is already terminal is left untouched (terminal is absorbing)."""
        fold = fold_agent_session_state(current=session.state, proposed=AgentSessionState.EXPIRED)
        if fold.outcome is not FoldOutcome.APPLIED:
            return
        repo = AgentSessionRepository(
            cast(TenantSession, _TenantShim(session_conn, organization_id))
        )
        updated = await repo.apply(
            session.id,
            {
                "state": fold.state.value,
                "state_rank": session_rank(fold.state),
                "disposition": (fold.disposition.value if fold.disposition else None),
                "error_code": "NXS_AGENT_SESSION_EXPIRED",
                "ended_at": dt.datetime.now(dt.UTC),
            },
        )
        assert updated is not None  # noqa: S101
        await self._emit_session_event(
            session_conn, organization_id, updated, "agent.session.expired"
        )
        await self._record_usage(session_conn, organization_id, updated)

    async def _run_turn(
        self,
        organization_id: UUID,
        session: AgentSession,
        turn: AgentTurn,
        request: SubmitTurnRequest,
        agent: AgentDefinition,
    ) -> TurnOutcome:
        profile = await self.get_profile(organization_id, agent.model_profile_id)
        account = await self.get_account(organization_id, profile.account_id)

        secret: Any = None
        if account.credential_ref is not None:
            with contextlib.suppress(NxsError):
                secret = await self._vault.get_secret(organization_id, account.credential_ref)

        async with self._db.tenant_transaction(organization_id) as tenant:
            prior = [
                PriorTurn(
                    sequence=t.sequence,
                    input_text=t.input_text,
                    response_text=t.response_text,
                )
                for t in await AgentTurnRepository(tenant).list_for_session(
                    session.id, limit=self._cfg.max_context_messages + 1
                )
                if t.id != turn.id and t.state is AgentTurnState.COMPLETED
            ]
            context = await self._context.build(
                tenant=tenant,
                session=session,
                prior_turns=prior,
                user_input=request.content,
                channel=session.channel.value,
            )

        allow_list = frozenset(agent.tool_keys)
        tool_specs = await self._tool_specs(organization_id, agent.tool_keys)
        principal = Principal(
            user_id=session.initiator_user_id,
            session_id=session.initiator_session_id,
            organization_id=organization_id,
            token_id=uuid.uuid7(),
            issued_at=session.created_at,
            expires_at=session.created_at + dt.timedelta(seconds=self._cfg.max_session_seconds),
        )
        ctx = TurnContext(
            model=profile.model,
            provider_api_base=account.api_base,
            agent_instructions=agent.system_instructions,
            temperature=agent.temperature if agent.temperature is not None else profile.temperature,
            max_output_tokens=(
                agent.max_output_tokens
                if agent.max_output_tokens is not None
                else profile.max_output_tokens
            ),
            tool_specs=tool_specs,
            allow_list=allow_list,
            max_tool_iterations=min(agent.max_tool_iterations, self._cfg.max_tool_iterations),
            principal=principal,
            correlation_id=request.correlation_id or session.correlation_id,
            idempotency_seed=f"{session.id}:{turn.sequence}",
            context=context,
            channel=session.channel.value,
        )
        adapter = self._provider_factory(account.provider)

        tool_seq = {"n": 0}

        async def _on_tool(call_id: str, outcome: ToolCallOutcome, iteration: int) -> None:
            tool_seq["n"] += 1
            await self._record_tool_call(
                organization_id, session, turn, outcome, iteration, tool_seq["n"], call_id
            )

        return await self._runtime.run_turn(
            ctx=ctx, adapter=adapter, secret=secret, http=self._http, on_tool=_on_tool
        )

    async def _tool_specs(
        self, organization_id: UUID, tool_keys: tuple[str, ...]
    ) -> tuple[ModelToolSpec, ...]:
        specs: list[ModelToolSpec] = []
        for key in tool_keys:
            try:
                tool = await self._tool_registry.get_by_key(organization_id, key)
            except NxsError:
                continue
            specs.append(
                ModelToolSpec(
                    name=tool.tool_key,
                    description=tool.description or tool.name,
                    parameters=tool.input_schema,
                )
            )
        return tuple(specs)

    async def _finish_turn(
        self,
        organization_id: UUID,
        session: AgentSession,
        turn: AgentTurn,
        request: SubmitTurnRequest,
        outcome: TurnOutcome,
    ) -> AgentResponse:
        now = dt.datetime.now(dt.UTC)
        content = outcome.content[: self._cfg.max_output_chars]
        stale_state: AgentSessionState | None = None
        async with self._db.tenant_transaction(organization_id) as tenant:
            session_repo = AgentSessionRepository(tenant)
            refreshed = await session_repo.by_id(session.id, for_update=True)
            assert refreshed is not None  # noqa: S101
            # DATABASE-level terminal absorption: a concurrent worker / replica may have
            # terminalised this session (EXPIRED / CANCELLED / FAILED / COMPLETED) while
            # this turn's model task was running. A terminal state is absorbing at the
            # transition boundary, not merely through the process-local asyncio lock — so
            # the stale outcome is discarded, the session is NOT resurrected, no
            # response_text is written and NO agent.response.ready is published.
            if session_is_terminal(refreshed.state):
                await self._discard_stale_turn(tenant, organization_id, turn, refreshed, now)
                stale_state = refreshed.state
            else:
                turn_repo = AgentTurnRepository(tenant)
                updated_turn = await turn_repo.apply(
                    turn.id,
                    {
                        "state": AgentTurnState.COMPLETED.value,
                        "response_text": content,
                        "response_char_count": len(content),
                        "model": None,
                        "finish_reason": outcome.finish_reason,
                        "input_tokens": outcome.usage.input_tokens,
                        "output_tokens": outcome.usage.output_tokens,
                        "tool_iterations": outcome.tool_iterations,
                        "latency_ms": _elapsed_ms(turn.created_at, now),
                    },
                )
                assert updated_turn is not None  # noqa: S101
                live = fold_agent_session_state(
                    current=refreshed.state, proposed=AgentSessionState.ACTIVE
                ).state
                new_session = await session_repo.apply(
                    session.id,
                    {
                        "state": live.value,
                        "state_rank": session_rank(live),
                        "input_tokens": refreshed.input_tokens + outcome.usage.input_tokens,
                        "output_tokens": refreshed.output_tokens + outcome.usage.output_tokens,
                        "tool_call_count": refreshed.tool_call_count + len(outcome.tool_calls),
                        "last_activity_at": now,
                    },
                )
                assert new_session is not None  # noqa: S101
                await self._emit_turn_event(
                    tenant.session,
                    organization_id,
                    new_session,
                    updated_turn,
                    "agent.turn.completed",
                    {
                        "finish_reason": outcome.finish_reason,
                        "tool_iterations": outcome.tool_iterations,
                        "input_tokens": outcome.usage.input_tokens,
                        "output_tokens": outcome.usage.output_tokens,
                        "latency_ms": updated_turn.latency_ms,
                    },
                )
                await self._publisher.enqueue(
                    tenant.session,
                    EventEnvelope.create(
                        event_type="agent.response.ready",
                        event_version=1,
                        aggregate_type="agent_session",
                        aggregate_id=str(session.id),
                        producer=self._settings.service_name,
                        organization_id=organization_id,
                        correlation_id=ctx_correlation(request, session),
                        payload={
                            "session_id": str(session.id),
                            "turn_id": str(turn.id),
                            "channel": session.channel.value,
                            "response_char_count": len(content),
                            "finish_reason": outcome.finish_reason,
                            "correlation_id": ctx_correlation(request, session),
                        },
                    ),
                )
                await self._record_usage(tenant.session, organization_id, new_session)
                return AgentResponse(
                    session_id=session.id,
                    turn_id=turn.id,
                    content=content,
                    finish_reason=outcome.finish_reason,
                    tool_calls=len(outcome.tool_calls),
                    input_tokens=outcome.usage.input_tokens,
                    output_tokens=outcome.usage.output_tokens,
                    total_tokens=outcome.usage.total_tokens,
                    latency_ms=updated_turn.latency_ms,
                    correlation_id=ctx_correlation(request, session),
                )
        # reached only on the stale path — the discarded turn was committed above.
        if stale_state is AgentSessionState.EXPIRED:
            raise AgentSessionExpiredError(
                "the agent session reached its lifetime ceiling during the turn"
            )
        raise AgentInvalidStateError("the agent session ended before the turn could be committed")

    async def _discard_stale_turn(
        self,
        tenant: TenantSession,
        organization_id: UUID,
        turn: AgentTurn,
        session_row: AgentSession,
        now: dt.datetime,
    ) -> None:
        """The session went terminal (concurrently) while this turn ran. Mark the turn
        CANCELLED with the session's terminal reason and emit ``agent.turn.failed`` —
        within the caller's transaction. No ``response_text``, no session write, no
        ``agent.response.ready``, no usage (that was recorded at terminalisation)."""
        error_code = session_row.error_code or "NXS_AGENT_SESSION_EXPIRED"
        updated = await AgentTurnRepository(tenant).apply(
            turn.id,
            {
                "state": AgentTurnState.CANCELLED.value,
                "error_code": error_code,
                "latency_ms": _elapsed_ms(turn.created_at, now),
            },
        )
        assert updated is not None  # noqa: S101
        await self._publisher.enqueue(
            tenant.session,
            EventEnvelope.create(
                event_type="agent.turn.failed",
                event_version=1,
                aggregate_type="agent_session",
                aggregate_id=str(session_row.id),
                producer=self._settings.service_name,
                organization_id=organization_id,
                payload={
                    "session_id": str(session_row.id),
                    "turn_id": str(turn.id),
                    "sequence": updated.sequence,
                    "error_code": error_code,
                    "latency_ms": updated.latency_ms,
                    "correlation_id": session_row.correlation_id,
                },
            ),
        )

    async def _fail_turn(
        self,
        organization_id: UUID,
        session_id: UUID,
        turn_id: UUID,
        error_code: str,
        *,
        cancelled: bool = False,
    ) -> None:
        target = AgentTurnState.CANCELLED if cancelled else AgentTurnState.FAILED
        with contextlib.suppress(Exception):
            async with self._db.tenant_transaction(organization_id) as tenant:
                turn_repo = AgentTurnRepository(tenant)
                turn = await turn_repo.by_id(turn_id)
                if turn is None or turn.state in (
                    AgentTurnState.COMPLETED,
                    AgentTurnState.FAILED,
                    AgentTurnState.CANCELLED,
                ):
                    return
                now = dt.datetime.now(dt.UTC)
                updated = await turn_repo.apply(
                    turn_id,
                    {
                        "state": target.value,
                        "error_code": error_code,
                        "latency_ms": _elapsed_ms(turn.created_at, now),
                    },
                )
                assert updated is not None  # noqa: S101
                session_repo = AgentSessionRepository(tenant)
                session = await session_repo.by_id(session_id, for_update=True)
                if session is not None and not session_is_terminal(session.state):
                    await session_repo.apply(
                        session_id,
                        {
                            "state": AgentSessionState.ACTIVE.value,
                            "state_rank": session_rank(AgentSessionState.ACTIVE),
                            "last_activity_at": now,
                        },
                    )
                await self._publisher.enqueue(
                    tenant.session,
                    EventEnvelope.create(
                        event_type="agent.turn.failed",
                        event_version=1,
                        aggregate_type="agent_session",
                        aggregate_id=str(session_id),
                        producer=self._settings.service_name,
                        organization_id=organization_id,
                        payload={
                            "session_id": str(session_id),
                            "turn_id": str(turn_id),
                            "sequence": updated.sequence,
                            "error_code": error_code,
                            "latency_ms": updated.latency_ms,
                            "correlation_id": None,
                        },
                    ),
                )

    async def _record_tool_call(
        self,
        organization_id: UUID,
        session: AgentSession,
        turn: AgentTurn,
        outcome: ToolCallOutcome,
        iteration: int,
        sequence: int,
        call_id: str,
    ) -> None:
        status = (
            AgentToolCallStatus.DENIED
            if not outcome.allowed
            else (AgentToolCallStatus.COMPLETED if outcome.ok else AgentToolCallStatus.FAILED)
        )
        async with self._db.tenant_transaction(organization_id) as tenant:
            record = await AgentToolCallRepository(tenant).insert(
                {
                    "session_id": session.id,
                    "turn_id": turn.id,
                    "sequence": sequence,
                    "iteration": iteration,
                    "tool_key": outcome.tool_key,
                    "arguments_hash": outcome.arguments_hash,
                    "status": status.value,
                    "tool_result_class": outcome.result_class,
                    "tool_status_code": outcome.status_code,
                    "denied_reason": outcome.denied_reason,
                    "latency_ms": outcome.latency_ms,
                }
            )
            base = {
                "session_id": str(session.id),
                "turn_id": str(turn.id),
                "tool_call_id": str(record.id),
                "tool_key": outcome.tool_key,
                "iteration": iteration,
                "arguments_hash": outcome.arguments_hash,
                "correlation_id": session.correlation_id,
            }
            await self._publisher.enqueue(
                tenant.session,
                EventEnvelope.create(
                    event_type="agent.tool.requested",
                    event_version=1,
                    aggregate_type="agent_session",
                    aggregate_id=str(session.id),
                    producer=self._settings.service_name,
                    organization_id=organization_id,
                    correlation_id=session.correlation_id,
                    payload=dict(base),
                ),
            )
            if status is AgentToolCallStatus.COMPLETED:
                await self._publisher.enqueue(
                    tenant.session,
                    EventEnvelope.create(
                        event_type="agent.tool.completed",
                        event_version=1,
                        aggregate_type="agent_session",
                        aggregate_id=str(session.id),
                        producer=self._settings.service_name,
                        organization_id=organization_id,
                        correlation_id=session.correlation_id,
                        payload={
                            **base,
                            "result_class": outcome.result_class,
                            "status_code": outcome.status_code,
                            "latency_ms": outcome.latency_ms,
                        },
                    ),
                )
            else:
                await self._publisher.enqueue(
                    tenant.session,
                    EventEnvelope.create(
                        event_type="agent.tool.failed",
                        event_version=1,
                        aggregate_type="agent_session",
                        aggregate_id=str(session.id),
                        producer=self._settings.service_name,
                        organization_id=organization_id,
                        correlation_id=session.correlation_id,
                        payload={
                            **base,
                            "outcome": "DENIED" if not outcome.allowed else "FAILED",
                            "error_code": outcome.error_code,
                            "latency_ms": outcome.latency_ms,
                        },
                    ),
                )

    async def _record_usage(self, session: Any, organization_id: UUID, row: AgentSession) -> None:
        shim = cast(TenantSession, _TenantShim(session, organization_id))
        await ModelUsageRepository(shim).upsert(
            row.id,
            {
                "model": "",
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "total_tokens": row.input_tokens + row.output_tokens,
                "tool_call_count": row.tool_call_count,
                "turn_count": row.turn_count,
                "latency_ms_total": 0,
            },
        )
        await self._publisher.enqueue(
            session,
            EventEnvelope.create(
                event_type="agent.usage.recorded",
                event_version=1,
                aggregate_type="agent_session",
                aggregate_id=str(row.id),
                producer=self._settings.service_name,
                organization_id=organization_id,
                correlation_id=row.correlation_id,
                payload={
                    "session_id": str(row.id),
                    "model": "",
                    "input_tokens": row.input_tokens,
                    "output_tokens": row.output_tokens,
                    "total_tokens": row.input_tokens + row.output_tokens,
                    "tool_call_count": row.tool_call_count,
                    "turn_count": row.turn_count,
                    "latency_ms_total": 0,
                    "correlation_id": row.correlation_id,
                },
            ),
        )

    async def _assert_references(self, tenant: Any, request: StartAgentSessionRequest) -> None:
        from nexus_ai.agents.errors import AgentNotAuthorizedError
        from nexus_ai.domain.customers.repository import (
            ConversationRepository,
            CustomerRepository,
        )

        if request.customer_id is not None and (
            await CustomerRepository(tenant).by_id(request.customer_id) is None
        ):
            raise AgentNotAuthorizedError("the referenced customer is not in this Organization")
        if request.conversation_id is not None and (
            await ConversationRepository(tenant).by_id(request.conversation_id) is None
        ):
            raise AgentNotAuthorizedError("the referenced conversation is not in this Organization")

    async def _replay_session(
        self, organization_id: UUID, idempotency_key: str, fingerprint: str
    ) -> AgentSession | None:
        if not idempotency_key:
            return None
        async with self._db.tenant_transaction(organization_id) as tenant:
            existing = await AgentSessionRepository(tenant).by_idempotency_key(idempotency_key)
        if existing is None:
            return None
        if existing.request_fingerprint != fingerprint:
            from nexus_ai.agents.errors import AgentIdempotencyConflictError

            raise AgentIdempotencyConflictError(
                "the idempotency key was used for a different session"
            )
        return existing

    async def _emit_session_event(
        self, session: Any, organization_id: UUID, row: AgentSession, event_type: str
    ) -> None:
        payload: dict[str, Any] = {
            "session_id": str(row.id),
            "agent_id": str(row.agent_id),
            "model_profile_id": str(row.model_profile_id),
            "channel": row.channel.value,
            "state": row.state.value,
            "correlation_id": row.correlation_id,
        }
        if event_type in ("agent.session.failed", "agent.session.expired"):
            payload["error_code"] = row.error_code
        await self._publisher.enqueue(
            session,
            EventEnvelope.create(
                event_type=event_type,
                event_version=1,
                aggregate_type="agent_session",
                aggregate_id=str(row.id),
                producer=self._settings.service_name,
                organization_id=organization_id,
                correlation_id=row.correlation_id,
                payload=payload,
            ),
        )

    async def _emit_turn_event(
        self,
        session: Any,
        organization_id: UUID,
        session_row: AgentSession,
        turn: AgentTurn,
        event_type: str,
        extra: dict[str, Any],
    ) -> None:
        payload = {
            "session_id": str(session_row.id),
            "turn_id": str(turn.id),
            "sequence": turn.sequence,
            "correlation_id": session_row.correlation_id,
            **extra,
        }
        await self._publisher.enqueue(
            session,
            EventEnvelope.create(
                event_type=event_type,
                event_version=1,
                aggregate_type="agent_session",
                aggregate_id=str(session_row.id),
                producer=self._settings.service_name,
                organization_id=organization_id,
                correlation_id=session_row.correlation_id,
                payload=payload,
            ),
        )

    def _response_from_turn(self, turn: AgentTurn) -> AgentResponse:
        return AgentResponse(
            session_id=turn.session_id,
            turn_id=turn.id,
            content=turn.response_text or "",
            finish_reason=turn.finish_reason or "STOP",
            tool_calls=turn.tool_iterations,
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
            total_tokens=turn.input_tokens + turn.output_tokens,
            latency_ms=turn.latency_ms,
            correlation_id=None,
        )

    def _require_enabled(self) -> None:
        if not self._cfg.enabled:
            raise AgentDisabledError("the agent runtime is disabled")

    async def shutdown(self) -> None:
        tasks = list(self._turn_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._turn_tasks.clear()
        self._locks.clear()

    async def join(self, session_id: UUID) -> None:
        task = self._turn_tasks.get(session_id)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


class _TenantShim:
    """Wraps a raw AsyncSession + org id as a TenantSession for repository reuse inside
    an already-open transaction."""

    def __init__(self, session: Any, organization_id: UUID) -> None:
        self.session = session
        self.organization_id = organization_id


def ctx_correlation(request: SubmitTurnRequest, session: AgentSession) -> str | None:
    return request.correlation_id or session.correlation_id


def _elapsed_ms(start: dt.datetime, end: dt.datetime) -> int:
    return max(0, int((end - start).total_seconds() * 1000))
