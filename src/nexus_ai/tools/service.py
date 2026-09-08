"""The Tool Engine invocation orchestrator (NXS-TOOL-001, ADR-0061).

``ToolEngine.invoke`` runs a single deterministic policy pipeline for every caller (an
LLM, the future Agent Runtime, the future Workflow Engine) — the caller chooses a
``tool_key`` and validated ``arguments`` and nothing else:

  1  tenant context (from the authenticated principal, never a payload)
  2  tool existence            -> NXS_TOOL_NOT_FOUND
  3  enabled state             -> NXS_TOOL_DISABLED
  4  version resolution
  5  caller principal
  6  server-side RBAC / required permissions   -> NXS_TOOL_PERMISSION_DENIED
  7  organization / tool policy (risk ceiling) -> NXS_TOOL_POLICY_DENIED
  8  argument JSON Schema      -> NXS_TOOL_ARGS_INVALID
  9  side-effect / risk policy (REQUIRED idempotency) -> NXS_TOOL_IDEMPOTENCY_REQUIRED
  10 durable idempotency claim / replay / conflict / in-progress
  11 integration binding + argument merge (static wins)
  12 governed Integration Hub execution (NXS-P07) — never bypassed, additionally bounded
     by ``tool.timeout_seconds`` when set (the stricter of the P08 and P07 limits wins)
     -> NXS_TOOL_TIMEOUT
  13 untrusted result validated against the output schema  -> NXS_TOOL_RESULT_INVALID
  14 execution receipt + P04 transactional event

No external connection is opened here — every external call is `IntegrationHubService.execute`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import time
from typing import Any
from uuid import UUID

from nexus_ai.core.config import Settings
from nexus_ai.core.context import current_context
from nexus_ai.core.errors import NxsError
from nexus_ai.core.logging import get_logger
from nexus_ai.domain.auth.entities import Principal
from nexus_ai.domain.tools.repository import ToolExecutionRepository
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.infrastructure.database import Database
from nexus_ai.integrations.entities import ExecutionRequest
from nexus_ai.integrations.service import IntegrationHubService
from nexus_ai.tools.entities import (
    SIDE_EFFECTING,
    RiskClass,
    ToolDefinition,
    ToolIdempotencyPolicy,
    ToolInvocation,
    ToolResult,
    ToolResultClass,
    ToolStatus,
)
from nexus_ai.tools.errors import (
    ToolDisabledError,
    ToolExecutionFailedError,
    ToolExecutionInProgressError,
    ToolIdempotencyConflictError,
    ToolIdempotencyRequiredError,
    ToolPolicyDeniedError,
    ToolResultInvalidError,
    ToolTimeoutError,
)
from nexus_ai.tools.idempotency import (
    ToolIdempotencyStatus,
    ToolIdempotencyStore,
    request_fingerprint,
    result_from_record,
)
from nexus_ai.tools.permissions import ToolPermissionGuard
from nexus_ai.tools.registry import ToolRegistry
from nexus_ai.tools.result import map_downstream_error, tool_result_class_for
from nexus_ai.tools.schemas import merge_arguments, validate_arguments, validate_output

_RISK_ORDER = {RiskClass.LOW: 0, RiskClass.MEDIUM: 1, RiskClass.HIGH: 2, RiskClass.CRITICAL: 3}


class ToolEngine:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        publisher: EventPublisher,
        registry: ToolRegistry,
        integration_hub: IntegrationHubService,
        permission_guard: ToolPermissionGuard,
        idempotency: ToolIdempotencyStore,
    ) -> None:
        self._settings = settings
        self._db = database
        self._publisher = publisher
        self._registry = registry
        self._hub = integration_hub
        self._permissions = permission_guard
        self._idem = idempotency
        self._log = get_logger("nexus_ai.tools.service")

    async def invoke(self, principal: Principal, invocation: ToolInvocation) -> ToolResult:
        organization_id = principal.organization_id  # 1 — trusted tenant context
        started = time.monotonic()

        tool = await self._registry.resolve_for_invocation(  # 2 — existence (RLS-scoped)
            organization_id, invocation.tool_key
        )
        if tool.status is not ToolStatus.ACTIVE:  # 3 — enabled state
            raise ToolDisabledError(
                "the tool is not ACTIVE", extensions={"status": tool.status.value}
            )
        # 4 — version resolution: the single active definition row / its version
        await self._authorize(organization_id, principal, tool)  # 5 + 6 — principal + RBAC
        self._check_policy(tool)  # 7 — organization / tool policy

        validate_arguments(invocation.arguments, tool.input_schema)  # 8 — argument schema
        merged = merge_arguments(invocation.arguments, tool.static_arguments)  # 11 — merge

        needs_key = (  # 9 — side-effect / risk policy
            tool.idempotency_policy is ToolIdempotencyPolicy.REQUIRED
            and tool.side_effect_class in SIDE_EFFECTING
        )
        if needs_key and invocation.idempotency_key is None:
            raise ToolIdempotencyRequiredError(
                "this tool requires an idempotency key for a repeatable side effect"
            )

        use_idempotency = (
            invocation.idempotency_key is not None
            and tool.idempotency_policy is not ToolIdempotencyPolicy.NONE
        )
        fingerprint = request_fingerprint(tool.tool_key, tool.version, merged)
        if use_idempotency:  # 10 — durable idempotency claim / replay
            replay = await self._claim(organization_id, tool, invocation, fingerprint)
            if replay is not None:
                return replay

        try:
            result = await self._run(  # 11 + 12 + 13
                organization_id, principal, tool, invocation, merged, started
            )
        except NxsError as exc:
            if use_idempotency and invocation.idempotency_key is not None:
                await self._idem.finalize(
                    organization_id=organization_id,
                    tool_id=tool.id,
                    idempotency_key=invocation.idempotency_key,
                    status=ToolIdempotencyStatus.FAILED,
                    result_json=None,
                    error_code=_code(exc),
                )
            raise

        if use_idempotency and invocation.idempotency_key is not None:
            await self._idem.finalize(
                organization_id=organization_id,
                tool_id=tool.id,
                idempotency_key=invocation.idempotency_key,
                status=ToolIdempotencyStatus.COMPLETED,
                result_json=result.model_dump(mode="json"),
                error_code=None,
            )
        return result

    # -- pipeline steps ----------------------------------------------------

    async def _authorize(
        self, organization_id: UUID, principal: Principal, tool: ToolDefinition
    ) -> None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            await self._permissions.require(tenant, principal, tool.required_permissions)

    def _check_policy(self, tool: ToolDefinition) -> None:
        ceiling = RiskClass(self._settings.tools.max_risk_class)
        if _RISK_ORDER[tool.risk_class] > _RISK_ORDER[ceiling]:
            raise ToolPolicyDeniedError(
                "the tool's risk class exceeds this Organization's ceiling",
                extensions={"risk_class": tool.risk_class.value, "ceiling": ceiling.value},
            )

    async def _run(
        self,
        organization_id: UUID,
        principal: Principal,
        tool: ToolDefinition,
        invocation: ToolInvocation,
        merged_arguments: dict[str, Any],
        started: float,
    ) -> ToolResult:
        derived_key = _derive_integration_key(tool.id, invocation.idempotency_key)
        request = ExecutionRequest(
            integration_id=tool.binding.integration_id,
            operation_key=tool.binding.operation_key,
            input=merged_arguments,
            idempotency_key=derived_key,
            correlation_id=invocation.correlation_id,
        )
        try:  # 12 — governed Integration Hub execution, bounded by the tool timeout
            downstream = await self._execute_bounded(organization_id, tool, request)
        except ToolTimeoutError as exc:  # the P08 tool timeout is the stricter bound
            await self._finish_failure(
                organization_id, principal, tool, invocation, exc, 0, started
            )
            raise
        except NxsError as exc:
            mapped = map_downstream_error(exc)
            await self._finish_failure(
                organization_id,
                principal,
                tool,
                invocation,
                mapped,
                0,
                started,
                downstream_code=_code(exc),
                downstream_status=_upstream_status(exc),
            )
            raise mapped from exc

        try:  # 13 — untrusted result validation
            validate_output(downstream.output, tool.output_schema)
        except ToolResultInvalidError as exc:
            await self._finish_failure(
                organization_id,
                principal,
                tool,
                invocation,
                exc,
                downstream.retry_count,
                started,
                downstream_code=downstream.error_code,
                downstream_status=downstream.status_code,
            )
            raise

        result = ToolResult(
            tool_key=tool.tool_key,
            tool_version=tool.version,
            result_class=ToolResultClass.SUCCESS,
            ok=True,
            status_code=downstream.status_code,
            output=downstream.output,
            retry_count=downstream.retry_count,
            duration_ms=int((time.monotonic() - started) * 1000),
            correlation_id=invocation.correlation_id or _correlation(),
            idempotency_key=invocation.idempotency_key,
        )
        await self._record(organization_id, principal, tool, result)  # 14 — receipt
        await self._emit_completed(organization_id, tool, result)
        return result

    async def _execute_bounded(
        self, organization_id: UUID, tool: ToolDefinition, request: ExecutionRequest
    ) -> Any:
        """Run the governed NXS-P07 execution, bounded by ``tool.timeout_seconds``.

        When the tool declares no timeout, the Integration Hub's own timeout / retry /
        circuit / rate-limit controls are the only bound. When it declares one, the Tool
        Engine additionally enforces it as an outer upper bound with the canonical async
        timeout primitive — P07's controls stay fully active, and whichever limit is
        stricter fires first. Expiry of the tool bound is a deterministic
        ``NXS_TOOL_TIMEOUT`` (never a bypass of P07, never a false success)."""
        if tool.timeout_seconds is None:
            return await self._hub.execute(organization_id, request)
        try:
            async with asyncio.timeout(tool.timeout_seconds):
                return await self._hub.execute(organization_id, request)
        except TimeoutError as exc:
            raise ToolTimeoutError(
                "the tool timeout elapsed before the governed execution completed",
                extensions={
                    "timeout_scope": "tool",
                    "timeout_seconds": tool.timeout_seconds,
                },
            ) from exc

    async def _claim(
        self,
        organization_id: UUID,
        tool: ToolDefinition,
        invocation: ToolInvocation,
        fingerprint: str,
    ) -> ToolResult | None:
        assert invocation.idempotency_key is not None  # noqa: S101 - caller-guarded
        expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(
            seconds=self._settings.tools.idempotency_retention_seconds
        )
        outcome = await self._idem.claim(
            organization_id=organization_id,
            tool_id=tool.id,
            idempotency_key=invocation.idempotency_key,
            request_fingerprint=fingerprint,
            expires_at=expires_at,
        )
        if outcome.is_owner:
            return None
        existing = outcome.existing
        if existing is None:  # pragma: no cover - a conflict implies a row
            raise ToolExecutionInProgressError("the idempotency claim is being finalised")
        if existing.request_fingerprint != fingerprint:
            raise ToolIdempotencyConflictError(
                "this idempotency key was already used with different arguments"
            )
        if existing.status is ToolIdempotencyStatus.COMPLETED:
            return result_from_record(existing, tool_key=tool.tool_key, tool_version=tool.version)
        if existing.status is ToolIdempotencyStatus.FAILED:
            raise ToolExecutionFailedError(
                "a previous invocation with this idempotency key failed; use a new key",
                extensions={
                    "original_error_code": existing.error_code or "NXS_TOOL_EXECUTION_FAILED"
                },
            )
        raise ToolExecutionInProgressError(
            "an invocation with this idempotency key is still running; retry shortly"
        )

    # -- bookkeeping -----------------------------------------------------

    async def _finish_failure(
        self,
        organization_id: UUID,
        principal: Principal,
        tool: ToolDefinition,
        invocation: ToolInvocation,
        error: NxsError,
        retry_count: int,
        started: float,
        *,
        downstream_code: str | None = None,
        downstream_status: int | None = None,
    ) -> None:
        code = _code(error)
        result = ToolResult(
            tool_key=tool.tool_key,
            tool_version=tool.version,
            result_class=tool_result_class_for(code),
            ok=False,
            status_code=error.status,
            error_code=code,
            error_detail=error.detail,
            downstream_code=downstream_code,
            downstream_status=downstream_status,
            retry_count=max(0, retry_count),
            duration_ms=int((time.monotonic() - started) * 1000),
            correlation_id=invocation.correlation_id or _correlation(),
            idempotency_key=invocation.idempotency_key,
        )
        await self._record(organization_id, principal, tool, result)
        await self._emit_failed(organization_id, tool, result)

    async def _record(
        self,
        organization_id: UUID,
        principal: Principal,
        tool: ToolDefinition,
        result: ToolResult,
    ) -> None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            await ToolExecutionRepository(tenant).record(
                tool_id=tool.id,
                tool_key=tool.tool_key,
                tool_version=result.tool_version,
                result_class=result.result_class.value,
                ok=result.ok,
                status_code=result.status_code,
                error_code=result.error_code,
                downstream_code=result.downstream_code,
                downstream_status=result.downstream_status,
                retry_count=result.retry_count,
                duration_ms=result.duration_ms,
                correlation_id=result.correlation_id,
                idempotency_key=result.idempotency_key,
                caller_user_id=principal.user_id,
            )

    async def _emit_completed(
        self, organization_id: UUID, tool: ToolDefinition, result: ToolResult
    ) -> None:
        await self._emit(
            organization_id,
            "tools.invocation.completed",
            tool.id,
            {
                "tool_id": str(tool.id),
                "tool_key": tool.tool_key,
                "tool_version": result.tool_version,
                "duration_ms": result.duration_ms,
                "replayed": result.replayed,
            },
        )

    async def _emit_failed(
        self, organization_id: UUID, tool: ToolDefinition, result: ToolResult
    ) -> None:
        await self._emit(
            organization_id,
            "tools.invocation.failed",
            tool.id,
            {
                "tool_id": str(tool.id),
                "tool_key": tool.tool_key,
                "tool_version": result.tool_version,
                "result_class": result.result_class.value,
                "error_code": result.error_code or "NXS_TOOL_EXECUTION_FAILED",
                "downstream_code": result.downstream_code,
            },
        )

    async def _emit(
        self, organization_id: UUID, event_type: str, tool_id: UUID, payload: dict[str, Any]
    ) -> None:
        ctx = current_context()
        envelope = EventEnvelope.create(
            event_type=event_type,
            event_version=1,
            aggregate_type="tool",
            aggregate_id=str(tool_id),
            producer=self._settings.service_name,
            organization_id=organization_id,
            correlation_id=None if ctx is None else ctx.correlation_id,
            payload=payload,
        )
        async with self._db.tenant_transaction(organization_id) as tenant:
            await self._publisher.enqueue(tenant.session, envelope)


def _derive_integration_key(tool_id: UUID, caller_key: str | None) -> str | None:
    if caller_key is None:
        return None
    digest = hashlib.sha256(f"{tool_id}:{caller_key}".encode()).hexdigest()[:40]
    return f"t-{digest}"


def _code(exc: Exception) -> str:
    return exc.code if isinstance(exc, NxsError) else "NXS_TOOL_EXECUTION_FAILED"


def _upstream_status(exc: NxsError) -> int | None:
    value = exc.extensions.get("upstream_status")
    return value if isinstance(value, int) else None


def _correlation() -> str | None:
    ctx = current_context()
    return None if ctx is None else ctx.correlation_id
