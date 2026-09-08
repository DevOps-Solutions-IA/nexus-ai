"""The Integration Hub execution orchestrator (NXS-INT-001, ADR-0054).

The single governed path for invoking an external system:

    ExecutionRequest (integration_id + operation_key + validated input)
      -> Registry (resolve ACTIVE integration + operation, config revision)
      -> operation-based request build (REST / GraphQL — validates the input, never a URL)
      -> durable idempotency claim
      -> outbound rate limit
      -> circuit breaker
      -> AuthProfile applied from the vault seam
      -> governed HTTP executor (SSRF re-validated, bounded, TLS-verified)
      -> retry policy (SAFE / IDEMPOTENT / NON_IDEMPOTENT + bounded backoff + Retry-After)
      -> untrusted response validation
      -> normalized IntegrationResult (+ execution record, + failure event)

The LLM is never on this path: it would call the future Tool Engine (P08), which would
call ``execute`` — it never reaches a raw URL and never receives a plaintext secret.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from nexus_ai.core.config import Settings
from nexus_ai.core.context import current_context
from nexus_ai.core.errors import NxsError
from nexus_ai.core.logging import get_logger
from nexus_ai.domain.integrations.repository import IntegrationExecutionRepository
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.infrastructure.database import Database
from nexus_ai.integrations.auth_profiles import AuthProfileApplier
from nexus_ai.integrations.backoff import FailureKind, RetryPolicy
from nexus_ai.integrations.circuit import CircuitBreakerRegistry
from nexus_ai.integrations.entities import (
    ExecutionRequest,
    GraphQLOperationSpec,
    IdempotencyMode,
    Integration,
    IntegrationOperation,
    IntegrationResult,
    OperationType,
    RestOperationSpec,
    ResultClass,
)
from nexus_ai.integrations.errors import (
    IntegrationExecutionFailedError,
    IntegrationExecutionInProgressError,
    IntegrationIdempotencyConflictError,
    IntegrationResponseInvalidError,
    IntegrationUpstreamClientError,
    IntegrationUpstreamRateLimitedError,
    IntegrationUpstreamServerError,
)
from nexus_ai.integrations.executor import (
    ExecutorFailure,
    GovernedHttpExecutor,
    OutboundRequest,
    RawResponse,
)
from nexus_ai.integrations.graphql import GraphQLInvocationBuilder
from nexus_ai.integrations.idempotency import (
    IdempotencyStatus,
    IdempotencyStore,
    request_fingerprint,
    result_from_record,
)
from nexus_ai.integrations.ratelimit import OutboundRateLimiter
from nexus_ai.integrations.registry import IntegrationRegistry
from nexus_ai.integrations.rest import RestInvocationBuilder
from nexus_ai.integrations.result import validate_graphql_response, validate_rest_response

_RESULT_CLASS_FOR_ERROR: dict[str, ResultClass] = {
    "NXS_INT_UPSTREAM_CLIENT_ERROR": ResultClass.UPSTREAM_CLIENT_ERROR,
    "NXS_INT_UPSTREAM_SERVER_ERROR": ResultClass.UPSTREAM_SERVER_ERROR,
    "NXS_INT_UPSTREAM_RATE_LIMITED": ResultClass.UPSTREAM_RATE_LIMITED,
    "NXS_INT_TIMEOUT": ResultClass.TRANSPORT_ERROR,
    "NXS_INT_CONNECTION_ERROR": ResultClass.TRANSPORT_ERROR,
    "NXS_INT_TLS_ERROR": ResultClass.TRANSPORT_ERROR,
    "NXS_INT_REDIRECT_BLOCKED": ResultClass.TRANSPORT_ERROR,
    "NXS_INT_RESPONSE_TOO_LARGE": ResultClass.RESPONSE_INVALID,
    "NXS_INT_RESPONSE_INVALID": ResultClass.RESPONSE_INVALID,
    "NXS_INT_DESTINATION_BLOCKED": ResultClass.POLICY_BLOCKED,
    "NXS_INT_CIRCUIT_OPEN": ResultClass.CIRCUIT_OPEN,
    "NXS_INT_OUTBOUND_RATE_LIMITED": ResultClass.RATE_LIMITED,
    "NXS_INT_IDEMPOTENCY_CONFLICT": ResultClass.IDEMPOTENCY_CONFLICT,
    "NXS_INT_CREDENTIAL_UNAVAILABLE": ResultClass.CONFIG_ERROR,
    "NXS_INT_AUTH_PROFILE_INVALID": ResultClass.CONFIG_ERROR,
    "NXS_INT_OPERATION_INPUT_INVALID": ResultClass.CONFIG_ERROR,
}

_RETRYABLE_UPSTREAM = (IntegrationUpstreamServerError, IntegrationUpstreamRateLimitedError)
_TERMINAL_UPSTREAM = (IntegrationUpstreamClientError, IntegrationResponseInvalidError)


@dataclass(slots=True)
class _Attempt:
    raw: RawResponse | None
    output: Any
    failure_kind: FailureKind | None
    error: NxsError | None
    retry_after: float | None = None


class IntegrationHubService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        publisher: EventPublisher,
        registry: IntegrationRegistry,
        executor: GovernedHttpExecutor,
        auth_applier: AuthProfileApplier,
        circuit: CircuitBreakerRegistry,
        rate_limiter: OutboundRateLimiter,
        idempotency: IdempotencyStore,
    ) -> None:
        self._settings = settings
        self._db = database
        self._publisher = publisher
        self._registry = registry
        self._executor = executor
        self._auth = auth_applier
        self._circuit = circuit
        self._rate = rate_limiter
        self._idem = idempotency
        self._retry = RetryPolicy(settings.integrations)
        self._rest = RestInvocationBuilder()
        self._graphql = GraphQLInvocationBuilder()
        self._log = get_logger("nexus_ai.integrations.service")

    async def execute(
        self, organization_id: UUID, request: ExecutionRequest, *, is_test: bool = False
    ) -> IntegrationResult:
        integration, operation = await self._registry.resolve_for_execution(
            organization_id, request.integration_id, request.operation_key
        )
        outbound = self._build_outbound(integration, operation, request.input)

        use_idempotency = (
            request.idempotency_key is not None
            and not is_test
            and self._idempotency_mode(request, operation) is not IdempotencyMode.NONE
        )
        if use_idempotency:
            fingerprint = request_fingerprint(request.operation_key, request.input)
            replay = await self._claim(organization_id, request, fingerprint)
            if replay is not None:
                return replay

        try:
            result = await self._run(
                organization_id, integration, operation, outbound, request, is_test=is_test
            )
        except NxsError as exc:
            if use_idempotency:
                await self._finalize(
                    organization_id, request, IdempotencyStatus.FAILED, None, _code(exc)
                )
            raise

        if use_idempotency:
            await self._finalize(
                organization_id,
                request,
                IdempotencyStatus.COMPLETED,
                result.model_dump(mode="json"),
                None,
            )
        return result

    # -- request building --------------------------------------------------

    def _build_outbound(
        self,
        integration: Integration,
        operation: IntegrationOperation,
        raw_input: dict[str, Any],
    ) -> OutboundRequest:
        spec = operation.spec
        if isinstance(spec, GraphQLOperationSpec):
            built = self._graphql.build(integration.base_url, spec, raw_input)
        elif isinstance(spec, RestOperationSpec):
            built = self._rest.build(integration.base_url, spec, raw_input)
        else:  # pragma: no cover - the discriminated union is exhaustive
            raise IntegrationExecutionFailedError("unknown operation type")
        return OutboundRequest(
            method=built.method,
            url=built.url,
            headers=built.headers,
            body=built.body,
            content_type=built.content_type,
            timeout_seconds=built.timeout_seconds,
            destination_rule=integration.destination_rule,
        )

    def _idempotency_mode(
        self, request: ExecutionRequest, operation: IntegrationOperation
    ) -> IdempotencyMode:
        return (
            IdempotencyMode.NONE if request.idempotency_key is None else IdempotencyMode.CALLER_KEY
        )

    # -- idempotency -----------------------------------------------------

    async def _claim(
        self, organization_id: UUID, request: ExecutionRequest, fingerprint: str
    ) -> IntegrationResult | None:
        assert request.idempotency_key is not None  # noqa: S101 - caller-guarded
        expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(
            seconds=self._settings.integrations.idempotency_retention_seconds
        )
        outcome = await self._idem.claim(
            organization_id=organization_id,
            integration_id=request.integration_id,
            operation_key=request.operation_key,
            idempotency_key=request.idempotency_key,
            request_fingerprint=fingerprint,
            expires_at=expires_at,
        )
        if outcome.is_owner:
            return None
        existing = outcome.existing
        if existing is None:  # pragma: no cover - a conflict implies a row
            raise IntegrationExecutionInProgressError("the idempotency claim is being finalised")
        if existing.request_fingerprint != fingerprint:
            raise IntegrationIdempotencyConflictError(
                "this idempotency key was already used with a different request"
            )
        if existing.status is IdempotencyStatus.COMPLETED:
            return result_from_record(
                existing,
                integration_id=request.integration_id,
                operation_key=request.operation_key,
            )
        if existing.status is IdempotencyStatus.FAILED:
            raise IntegrationExecutionFailedError(
                "a previous execution with this idempotency key failed; use a new key",
                extensions={
                    "original_error_code": existing.error_code or "NXS_INT_EXECUTION_FAILED"
                },
            )
        raise IntegrationExecutionInProgressError(
            "an execution with this idempotency key is still running; retry shortly"
        )

    async def _finalize(
        self,
        organization_id: UUID,
        request: ExecutionRequest,
        status: IdempotencyStatus,
        result_json: dict[str, Any] | None,
        error_code: str | None,
    ) -> None:
        assert request.idempotency_key is not None  # noqa: S101 - caller-guarded
        await self._idem.finalize(
            organization_id=organization_id,
            integration_id=request.integration_id,
            operation_key=request.operation_key,
            idempotency_key=request.idempotency_key,
            status=status,
            result_json=result_json,
            error_code=error_code,
        )

    # -- the guarded execution loop --------------------------------------

    async def _run(
        self,
        organization_id: UUID,
        integration: Integration,
        operation: IntegrationOperation,
        outbound: OutboundRequest,
        request: ExecutionRequest,
        *,
        is_test: bool,
    ) -> IntegrationResult:
        circuit_key = (organization_id, integration.id, operation.operation_key)
        await self._rate.check_and_consume(organization_id, integration.id)
        await self._circuit.before_call(circuit_key)

        prepared = await self._auth.prepare(organization_id, integration.auth_profile)
        outbound = _merge_auth(outbound, prepared.headers, prepared.query_params)
        retry_class = operation.spec.retry_class

        started = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            outcome = await self._attempt(outbound, operation)
            if outcome.failure_kind is None:
                await self._circuit.record_success(circuit_key)
                assert outcome.raw is not None  # noqa: S101 - success implies a response
                return await self._success_result(
                    organization_id,
                    integration,
                    operation,
                    request,
                    outcome.raw,
                    outcome.output,
                    attempt - 1,
                    started,
                    is_test=is_test,
                )
            decision = self._retry.decide(
                retry_class=retry_class,
                failure=outcome.failure_kind,
                attempt=attempt,
                elapsed_seconds=time.monotonic() - started,
                retry_after_seconds=outcome.retry_after,
            )
            if decision.should_retry:
                await asyncio.sleep(decision.delay_seconds)
                continue
            await self._circuit.record_failure(circuit_key)
            assert outcome.error is not None  # noqa: S101 - a failure carries an error
            await self._finish_failure(
                organization_id,
                integration,
                operation,
                request,
                outcome.error,
                attempt - 1,
                started,
                upstream_status=outcome.raw.status_code if outcome.raw else None,
                is_test=is_test,
            )
            raise outcome.error

    async def _attempt(
        self, outbound: OutboundRequest, operation: IntegrationOperation
    ) -> _Attempt:
        try:
            raw = await self._executor.send(outbound)
        except ExecutorFailure as failure:
            return _Attempt(None, None, failure.kind, _as_nxs(failure.public))
        except NxsError as exc:  # bounded terminal transport error (too large / redirect)
            return _Attempt(None, None, FailureKind.TERMINAL, exc)
        try:
            output = self._validate(raw, operation)
        except _RETRYABLE_UPSTREAM as exc:
            retry_after = None
            kind = FailureKind.UPSTREAM_5XX
            if isinstance(exc, IntegrationUpstreamRateLimitedError):
                kind = FailureKind.UPSTREAM_429
                after = exc.extensions.get("retry_after_seconds")
                retry_after = float(after) if isinstance(after, (int, float)) else None
            return _Attempt(raw, None, kind, exc, retry_after)
        except _TERMINAL_UPSTREAM as exc:
            return _Attempt(raw, None, FailureKind.TERMINAL, exc)
        return _Attempt(raw, output, None, None)

    def _validate(self, raw: RawResponse, operation: IntegrationOperation) -> Any:
        spec = operation.spec
        if operation.operation_type is OperationType.GRAPHQL and isinstance(
            spec, GraphQLOperationSpec
        ):
            return validate_graphql_response(raw, spec.response_schema)
        assert isinstance(spec, RestOperationSpec)  # noqa: S101 - REST otherwise
        return validate_rest_response(raw, spec)

    # -- result assembly -------------------------------------------------

    async def _success_result(
        self,
        organization_id: UUID,
        integration: Integration,
        operation: IntegrationOperation,
        request: ExecutionRequest,
        raw: RawResponse,
        output: Any,
        retry_count: int,
        started: float,
        *,
        is_test: bool,
    ) -> IntegrationResult:
        result = IntegrationResult(
            integration_id=integration.id,
            operation_key=operation.operation_key,
            result_class=ResultClass.SUCCESS,
            ok=True,
            status_code=raw.status_code,
            output=output,
            upstream_status=raw.status_code,
            retry_count=max(0, retry_count),
            duration_ms=int((time.monotonic() - started) * 1000),
            config_revision=integration.config_revision,
            correlation_id=request.correlation_id or _correlation(),
            idempotency_key=request.idempotency_key,
        )
        if not is_test:
            await self._record(organization_id, integration, operation, result)
        return result

    async def _finish_failure(
        self,
        organization_id: UUID,
        integration: Integration,
        operation: IntegrationOperation,
        request: ExecutionRequest,
        error: NxsError,
        retry_count: int,
        started: float,
        *,
        upstream_status: int | None = None,
        is_test: bool,
    ) -> None:
        if is_test:
            return
        code = _code(error)
        result = IntegrationResult(
            integration_id=integration.id,
            operation_key=operation.operation_key,
            result_class=_RESULT_CLASS_FOR_ERROR.get(code, ResultClass.EXECUTION_FAILED),
            ok=False,
            status_code=error.status,
            upstream_status=upstream_status,
            error_code=code,
            error_detail=error.detail,
            retry_count=max(0, retry_count),
            duration_ms=int((time.monotonic() - started) * 1000),
            config_revision=integration.config_revision,
            correlation_id=request.correlation_id or _correlation(),
            idempotency_key=request.idempotency_key,
        )
        await self._record(organization_id, integration, operation, result)
        await self._emit_failure(organization_id, integration, operation, result)

    async def _record(
        self,
        organization_id: UUID,
        integration: Integration,
        operation: IntegrationOperation,
        result: IntegrationResult,
    ) -> None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            await IntegrationExecutionRepository(tenant).record(
                integration_id=integration.id,
                operation_key=operation.operation_key,
                config_revision=result.config_revision,
                result_class=result.result_class.value,
                ok=result.ok,
                status_code=result.status_code,
                upstream_status=result.upstream_status,
                error_code=result.error_code,
                retry_count=result.retry_count,
                duration_ms=result.duration_ms,
                correlation_id=result.correlation_id,
                idempotency_key=result.idempotency_key,
            )

    async def _emit_failure(
        self,
        organization_id: UUID,
        integration: Integration,
        operation: IntegrationOperation,
        result: IntegrationResult,
    ) -> None:
        ctx = current_context()
        envelope = EventEnvelope.create(
            event_type="integrations.execution.failed",
            event_version=1,
            aggregate_type="integration",
            aggregate_id=str(integration.id),
            producer=self._settings.service_name,
            organization_id=organization_id,
            correlation_id=None if ctx is None else ctx.correlation_id,
            payload={
                "integration_id": str(integration.id),
                "operation_key": operation.operation_key,
                "result_class": result.result_class.value,
                "error_code": result.error_code or "NXS_INT_EXECUTION_FAILED",
                "upstream_status": result.upstream_status,
                "config_revision": result.config_revision,
            },
        )
        async with self._db.tenant_transaction(organization_id) as tenant:
            await self._publisher.enqueue(tenant.session, envelope)


def _merge_auth(
    outbound: OutboundRequest, headers: dict[str, str], query_params: dict[str, str]
) -> OutboundRequest:
    merged_headers = {**dict(outbound.headers), **headers}
    url = outbound.url
    if query_params:
        parts = urlsplit(url)
        pairs = parse_qsl(parts.query, keep_blank_values=True) + list(query_params.items())
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), ""))
    return OutboundRequest(
        method=outbound.method,
        url=url,
        headers=merged_headers,
        body=outbound.body,
        content_type=outbound.content_type,
        timeout_seconds=outbound.timeout_seconds,
        destination_rule=outbound.destination_rule,
    )


def _as_nxs(exc: Exception) -> NxsError:
    if isinstance(exc, NxsError):
        return exc
    return IntegrationExecutionFailedError(str(exc))  # pragma: no cover - executor wraps its errors


def _code(exc: Exception) -> str:
    return exc.code if isinstance(exc, NxsError) else "NXS_INT_EXECUTION_FAILED"


def _correlation() -> str | None:
    ctx = current_context()
    return None if ctx is None else ctx.correlation_id
