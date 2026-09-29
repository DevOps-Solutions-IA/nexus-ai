"""PostgreSQL-authoritative tenant controls with one explicit lock ordering."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert

from nexus_ai.audit.context import actor_scope, require_actor
from nexus_ai.audit.contracts import AuditActor, AuditMetadata
from nexus_ai.audit.producer import emit_audit
from nexus_ai.compliance.contracts import (
    DESTRUCTIVE,
    INCOMPLETE_RESOURCES,
    REQUEST_ACTION,
    Action,
    ApprovalInput,
    ClaimInput,
    ComplianceConflict,
    ExecutionFence,
    HoldInput,
    PolicyInput,
    RequestKind,
    ResourceClass,
    RetentionInput,
    RetentionRule,
    SubjectRequestInput,
    VerificationInput,
    fingerprint,
    require_transition,
    retention_due,
)
from nexus_ai.compliance.events import ComplianceStateChangedV1
from nexus_ai.core.errors import DependencyUnavailableError, NotFoundError, PermissionDeniedError
from nexus_ai.domain.auth.entities import Principal
from nexus_ai.domain.auth.models import RefreshSessionRecord
from nexus_ai.domain.auth.rbac import AuthorizationService, PermissionKey
from nexus_ai.domain.auth.state import PrincipalStateValidator
from nexus_ai.domain.compliance.models import (
    ComplianceApproval,
    ComplianceControl,
    ComplianceExecution,
    ComplianceHold,
    CompliancePlan,
    CompliancePolicy,
    ComplianceRequest,
)
from nexus_ai.domain.customers.compliance import CustomerProfileAdapter
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.infrastructure.database import Database
from nexus_ai.infrastructure.tenant_session import TenantSession


def plan_semantics(plan: CompliancePlan) -> dict[str, Any]:
    return {
        "organization_id": str(plan.organization_id),
        "request_id": str(plan.request_id) if plan.request_id else None,
        "subject_id": str(plan.subject_id),
        "target_id": str(plan.subject_id),
        "policy_id": str(plan.policy_id),
        "policy_revision": plan.policy_revision,
        "policy_epoch": plan.policy_epoch,
        "hold_epoch": plan.hold_epoch,
        "target_version": plan.target_version,
        "resource_class": plan.resource_class,
        "action": plan.action,
        "operation_identity": plan.operation_identity,
        "parameters": {},
    }


def request_view(row: ComplianceRequest) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "subject_id": str(row.subject_id),
        "kind": row.kind,
        "state": row.state,
        "incomplete_resources": row.incomplete_resources,
        "result": row.result,
    }


class ComplianceService:
    def __init__(self, database: Database, publisher: EventPublisher, *, service_name: str) -> None:
        self._db = database
        self._publisher = publisher
        self._service_name = service_name
        self._authorizer = AuthorizationService(database)
        self._principal_validator = PrincipalStateValidator(database)
        self._profile = CustomerProfileAdapter()

    @asynccontextmanager
    async def _transaction(
        self,
        principal: Principal,
        capability: PermissionKey,
    ) -> AsyncIterator[tuple[TenantSession, ComplianceControl]]:
        if not self._db.is_connected:
            raise DependencyUnavailableError("compliance database authority is unavailable")
        if principal.expires_at <= dt.datetime.now(dt.UTC):
            raise PermissionDeniedError("expired compliance principal")
        await self._principal_validator.require_valid(
            user_id=principal.user_id,
            session_id=principal.session_id,
            organization_id=principal.organization_id,
        )
        async with self._db.tenant_transaction(principal.organization_id) as tenant:
            session = await tenant.session.get(RefreshSessionRecord, principal.session_id)
            if session is None or session.user_id != principal.user_id:
                raise PermissionDeniedError("principal does not own session")
            await self._authorizer.require(principal, capability, tenant=tenant)
            await tenant.session.execute(
                insert(ComplianceControl)
                .values(
                    id=principal.organization_id,
                    organization_id=principal.organization_id,
                    policy_epoch=0,
                    hold_epoch=0,
                )
                .on_conflict_do_nothing(index_elements=["organization_id"])
            )
            control = await tenant.session.scalar(select(ComplianceControl).with_for_update())
            if control is None:
                raise PermissionDeniedError("missing tenant compliance authority")
            await tenant.session.refresh(session)
            await self._principal_validator.require_valid_in(
                tenant,
                user_id=principal.user_id,
                session_id=principal.session_id,
            )
            if principal.expires_at <= await self._now(tenant):
                raise PermissionDeniedError("expired compliance principal")
            await self._authorizer.require(principal, capability, tenant=tenant)
            with actor_scope(
                principal.organization_id,
                AuditActor(
                    kind="HUMAN", user_id=principal.user_id, session_id=principal.session_id
                ),
            ):
                yield tenant, control

    async def _event(
        self, tenant: TenantSession, identity: uuid.UUID, kind: str, state: str
    ) -> None:
        payload = ComplianceStateChangedV1(resource_id=identity, resource_type=kind, state=state)
        await tenant.session.flush()
        await emit_audit(
            tenant,
            producer="compliance",
            action="compliance.state.changed",
            target_type=f"compliance_{kind}",
            target_id=identity,
            actor=require_actor(tenant.organization_id),
            metadata=AuditMetadata.model_validate({"state": state}),
        )
        await self._publisher.enqueue(
            tenant.session,
            EventEnvelope.create(
                event_type=payload.EVENT_TYPE,
                event_version=1,
                aggregate_type=f"compliance_{kind}",
                aggregate_id=str(identity),
                producer=self._service_name,
                organization_id=tenant.organization_id,
                payload=payload.model_dump(mode="json"),
            ),
        )

    @staticmethod
    async def _now(tenant: TenantSession) -> dt.datetime:
        value = await tenant.session.scalar(select(func.clock_timestamp()))
        if not isinstance(value, dt.datetime):
            raise ComplianceConflict("database time unavailable")
        return value

    @staticmethod
    async def _request(tenant: TenantSession, identity: uuid.UUID) -> ComplianceRequest:
        row = await tenant.session.scalar(
            select(ComplianceRequest).where(ComplianceRequest.id == identity).with_for_update()
        )
        if row is None:
            raise NotFoundError("subject request not found")
        return row

    @staticmethod
    async def _plan(tenant: TenantSession, identity: uuid.UUID) -> CompliancePlan:
        row = await tenant.session.get(CompliancePlan, identity)
        if row is None:
            raise NotFoundError("compliance plan not found")
        return row

    @staticmethod
    async def _active(tenant: TenantSession) -> CompliancePolicy:
        row = await tenant.session.scalar(
            select(CompliancePolicy).where(CompliancePolicy.state == "ACTIVE")
        )
        if row is None:
            raise ComplianceConflict("no active compliance policy")
        return row

    async def create_policy(self, principal: Principal, payload: PolicyInput) -> dict[str, Any]:
        async with self._transaction(principal, PermissionKey.COMPLIANCE_MANAGE_POLICY) as (
            tenant,
            _,
        ):
            previous = await tenant.session.scalar(select(func.max(CompliancePolicy.revision)))
            row = CompliancePolicy(
                id=uuid.uuid7(),
                organization_id=tenant.organization_id,
                revision=(previous or 0) + 1,
                state="DRAFT",
                rules=[rule.model_dump(mode="json") for rule in payload.rules],
            )
            tenant.session.add(row)
            await self._event(tenant, row.id, "policy", "DRAFT")
            return {"id": str(row.id), "revision": row.revision, "state": row.state}

    async def activate_policy(self, principal: Principal, identity: uuid.UUID) -> dict[str, Any]:
        async with self._transaction(principal, PermissionKey.COMPLIANCE_MANAGE_POLICY) as (
            tenant,
            control,
        ):
            row = await tenant.session.get(CompliancePolicy, identity)
            if row is None:
                raise NotFoundError("policy not found")
            if row.state != "DRAFT":
                raise ComplianceConflict("only draft policy may activate")
            previous = await tenant.session.scalar(
                select(CompliancePolicy).where(CompliancePolicy.state == "ACTIVE")
            )
            if previous is not None:
                previous.state = "RETIRED"
                await self._event(tenant, previous.id, "policy", "RETIRED")
            row.state = "ACTIVE"
            row.activated_by = principal.user_id
            row.activated_at = await self._now(tenant)
            control.policy_epoch += 1
            await self._event(tenant, row.id, "policy", "ACTIVE")
            return {"id": str(row.id), "revision": row.revision, "state": row.state}

    async def retire_policy(self, principal: Principal, identity: uuid.UUID) -> None:
        async with self._transaction(principal, PermissionKey.COMPLIANCE_MANAGE_POLICY) as (
            tenant,
            control,
        ):
            row = await tenant.session.get(CompliancePolicy, identity)
            if row is None:
                raise NotFoundError("policy not found")
            if row.state != "ACTIVE":
                raise ComplianceConflict("only active policy may retire")
            row.state = "RETIRED"
            control.policy_epoch += 1
            await self._event(tenant, row.id, "policy", "RETIRED")

    async def create_hold(self, principal: Principal, payload: HoldInput) -> dict[str, Any]:
        async with self._transaction(principal, PermissionKey.COMPLIANCE_MANAGE_HOLD) as (
            tenant,
            control,
        ):
            await self._profile.locked_target(tenant, payload.subject_id)
            row = ComplianceHold(
                id=uuid.uuid7(),
                organization_id=tenant.organization_id,
                subject_id=payload.subject_id,
                resource_class=payload.resource_class,
                state="ACTIVE",
                reason_code=payload.reason_code,
                created_by=principal.user_id,
            )
            tenant.session.add(row)
            control.hold_epoch += 1
            await self._event(tenant, row.id, "hold", "ACTIVE")
            return {"id": str(row.id), "state": row.state}

    async def release_hold(self, principal: Principal, identity: uuid.UUID) -> None:
        async with self._transaction(principal, PermissionKey.COMPLIANCE_MANAGE_HOLD) as (
            tenant,
            control,
        ):
            row = await tenant.session.get(ComplianceHold, identity)
            if row is None:
                raise NotFoundError("hold not found")
            if row.state != "ACTIVE":
                raise ComplianceConflict("hold already released")
            row.state = "RELEASED"
            row.released_by = principal.user_id
            row.released_at = await self._now(tenant)
            control.hold_epoch += 1
            await self._event(tenant, row.id, "hold", "RELEASED")

    async def create_request(
        self, principal: Principal, payload: SubjectRequestInput
    ) -> dict[str, Any]:
        async with self._transaction(principal, PermissionKey.COMPLIANCE_MANAGE_REQUEST) as (
            tenant,
            _,
        ):
            digest = fingerprint(payload.model_dump(mode="json"))
            row = await tenant.session.scalar(
                select(ComplianceRequest).where(
                    ComplianceRequest.idempotency_key == payload.idempotency_key,
                )
            )
            if row is not None:
                if row.semantic_digest != digest:
                    raise ComplianceConflict(
                        "idempotency identity already binds different semantics"
                    )
                return request_view(row)
            await self._profile.locked_target(tenant, payload.subject_id)
            row = ComplianceRequest(
                id=uuid.uuid7(),
                organization_id=tenant.organization_id,
                subject_id=payload.subject_id,
                kind=payload.kind,
                state="RECEIVED",
                idempotency_key=payload.idempotency_key,
                semantic_digest=digest,
                incomplete_resources=list(INCOMPLETE_RESOURCES),
                result={},
                verification={},
            )
            tenant.session.add(row)
            await self._event(tenant, row.id, "subject_request", "RECEIVED")
            return request_view(row)

    async def verify_request(
        self, principal: Principal, identity: uuid.UUID, payload: VerificationInput
    ) -> dict[str, Any]:
        async with self._transaction(principal, PermissionKey.COMPLIANCE_MANAGE_REQUEST) as (
            tenant,
            _,
        ):
            row = await self._request(tenant, identity)
            require_transition(row.state, "VERIFIED")
            row.state = "VERIFIED"
            row.verified_by = principal.user_id
            row.verified_at = await self._now(tenant)
            row.verification = payload.model_dump(mode="json")
            await self._event(tenant, row.id, "subject_request", row.state)
            return request_view(row)

    async def end_request(
        self, principal: Principal, identity: uuid.UUID, target: str
    ) -> dict[str, Any]:
        if target not in {"DENIED", "CANCELLED", "EXPIRED"}:
            raise ComplianceConflict("unsupported terminal decision")
        async with self._transaction(principal, PermissionKey.COMPLIANCE_MANAGE_REQUEST) as (
            tenant,
            _,
        ):
            row = await self._request(tenant, identity)
            require_transition(row.state, target)
            row.state = target
            await self._event(tenant, row.id, "subject_request", row.state)
            return request_view(row)

    async def get_request(self, principal: Principal, identity: uuid.UUID) -> dict[str, Any]:
        async with self._transaction(principal, PermissionKey.COMPLIANCE_READ) as (tenant, _):
            return request_view(await self._request(tenant, identity))

    async def list_requests(
        self, principal: Principal, *, after: uuid.UUID | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ComplianceConflict("page size out of bounds")
        async with self._transaction(principal, PermissionKey.COMPLIANCE_READ) as (tenant, _):
            query = select(ComplianceRequest).order_by(ComplianceRequest.id).limit(limit)
            if after is not None:
                query = query.where(ComplianceRequest.id > after)
            return [request_view(row) for row in (await tenant.session.scalars(query)).all()]

    @staticmethod
    async def _holds(tenant: TenantSession, subject: uuid.UUID, action: Action) -> None:
        if action not in DESTRUCTIVE:
            return
        hold = await tenant.session.scalar(
            select(ComplianceHold.id)
            .where(
                ComplianceHold.subject_id == subject,
                ComplianceHold.state == "ACTIVE",
                or_(
                    ComplianceHold.resource_class.is_(None),
                    ComplianceHold.resource_class == ResourceClass.CUSTOMER_PROFILE,
                ),
            )
            .limit(1)
        )
        if hold is not None:
            raise ComplianceConflict("active legal hold blocks destructive action")

    async def _new_plan(
        self,
        tenant: TenantSession,
        control: ComplianceControl,
        subject: uuid.UUID,
        action: Action,
        operation: str,
        request_id: uuid.UUID | None,
    ) -> CompliancePlan:
        policy = await self._active(tenant)
        if not any(
            rule["resource_class"] == ResourceClass.CUSTOMER_PROFILE for rule in policy.rules
        ):
            raise ComplianceConflict("policy does not cover customer profile")
        await self._holds(tenant, subject, action)
        target = await self._profile.locked_target(tenant, subject)
        row = CompliancePlan(
            id=uuid.uuid7(),
            organization_id=tenant.organization_id,
            request_id=request_id,
            policy_id=policy.id,
            policy_revision=policy.revision,
            policy_epoch=control.policy_epoch,
            hold_epoch=control.hold_epoch,
            subject_id=subject,
            target_version=target.version,
            resource_class=ResourceClass.CUSTOMER_PROFILE,
            action=action,
            operation_identity=operation,
        )
        row.fingerprint = fingerprint(plan_semantics(row))
        tenant.session.add(row)
        await tenant.session.flush()
        return row

    async def plan_request(self, principal: Principal, identity: uuid.UUID) -> dict[str, Any]:
        async with self._transaction(principal, PermissionKey.COMPLIANCE_MANAGE_REQUEST) as (
            tenant,
            control,
        ):
            request = await self._request(tenant, identity)
            require_transition(request.state, "PLANNED")
            plan = await self._new_plan(
                tenant,
                control,
                request.subject_id,
                REQUEST_ACTION[RequestKind(request.kind)],
                f"request:{request.id}",
                request.id,
            )
            request.state = "PLANNED"
            await self._event(tenant, request.id, "subject_request", request.state)
            return {"id": str(plan.id), "fingerprint": plan.fingerprint, "action": plan.action}

    async def evaluate_retention(
        self, principal: Principal, payload: RetentionInput
    ) -> dict[str, Any]:
        if payload.resource_class != ResourceClass.CUSTOMER_PROFILE:
            raise ComplianceConflict("unsupported retention adapter")
        async with self._transaction(principal, PermissionKey.COMPLIANCE_MANAGE_REQUEST) as (
            tenant,
            control,
        ):
            operation = f"retention:{payload.idempotency_key}"
            existing = await tenant.session.scalar(
                select(CompliancePlan).where(CompliancePlan.operation_identity == operation)
            )
            if existing is not None:
                if existing.subject_id != payload.subject_id:
                    raise ComplianceConflict("retention idempotency conflict")
                return {"id": str(existing.id), "action": existing.action}
            policy = await self._active(tenant)
            rules = [
                RetentionRule.model_validate(rule)
                for rule in policy.rules
                if rule["resource_class"] == payload.resource_class
            ]
            if not rules:
                raise ComplianceConflict("resource not governed by policy")
            target = await self._profile.locked_target(tenant, payload.subject_id)
            action = retention_due(target.created_at, await self._now(tenant), rules[0])
            if action == Action.RETAIN:
                return {
                    "action": action.value,
                    "policy_id": str(policy.id),
                    "policy_revision": policy.revision,
                }
            plan = await self._new_plan(
                tenant, control, payload.subject_id, action, operation, None
            )
            return {"id": str(plan.id), "fingerprint": plan.fingerprint, "action": plan.action}

    async def _revalidate(
        self, tenant: TenantSession, control: ComplianceControl, plan: CompliancePlan
    ) -> None:
        policy = await self._active(tenant)
        if (
            plan.policy_id != policy.id
            or plan.policy_revision != policy.revision
            or plan.policy_epoch != control.policy_epoch
            or plan.hold_epoch != control.hold_epoch
            or plan.fingerprint != fingerprint(plan_semantics(plan))
        ):
            raise ComplianceConflict("policy, hold or fingerprint changed")
        if plan.request_id is not None:
            request = await self._request(tenant, plan.request_id)
            if (
                request.state not in {"PLANNED", "APPROVED", "EXECUTING"}
                or request.verified_by is None
            ):
                raise ComplianceConflict("request is not authorized for execution")
        await self._holds(tenant, plan.subject_id, Action(plan.action))
        target = await self._profile.locked_target(tenant, plan.subject_id)
        if target.version != plan.target_version:
            raise ComplianceConflict("target version changed")

    async def approve(
        self, principal: Principal, identity: uuid.UUID, payload: ApprovalInput
    ) -> dict[str, Any]:
        async with self._transaction(principal, PermissionKey.COMPLIANCE_APPROVE) as (
            tenant,
            control,
        ):
            plan = await self._plan(tenant, identity)
            await self._revalidate(tenant, control, plan)
            existing = await tenant.session.scalar(
                select(ComplianceApproval).where(ComplianceApproval.plan_id == plan.id)
            )
            if existing is not None:
                raise ComplianceConflict("plan already has immutable approval")
            now = await self._now(tenant)
            approval = ComplianceApproval(
                id=uuid.uuid7(),
                organization_id=tenant.organization_id,
                plan_id=plan.id,
                fingerprint=plan.fingerprint,
                approved_by=principal.user_id,
                reason_code=payload.reason_code,
                issued_at=now,
                expires_at=now + dt.timedelta(seconds=payload.lifetime_seconds),
            )
            tenant.session.add(approval)
            if plan.request_id is not None:
                request = await self._request(tenant, plan.request_id)
                require_transition(request.state, "APPROVED")
                request.state = "APPROVED"
            await self._event(tenant, plan.id, "approval", "APPROVED")
            return {"id": str(approval.id), "fingerprint": approval.fingerprint}

    async def _approval(self, tenant: TenantSession, plan: CompliancePlan) -> None:
        approval = await tenant.session.scalar(
            select(ComplianceApproval).where(ComplianceApproval.plan_id == plan.id)
        )
        if (
            approval is None
            or approval.fingerprint != plan.fingerprint
            or approval.expires_at <= await self._now(tenant)
        ):
            raise ComplianceConflict("missing, stale or expired approval")

    async def claim(
        self, principal: Principal, identity: uuid.UUID, payload: ClaimInput
    ) -> dict[str, Any]:
        async with self._transaction(principal, PermissionKey.COMPLIANCE_EXECUTE) as (
            tenant,
            control,
        ):
            plan = await self._plan(tenant, identity)
            execution = await tenant.session.scalar(
                select(ComplianceExecution).where(ComplianceExecution.plan_id == plan.id)
            )
            if execution is not None and execution.state != "CLAIMED":
                return self._execution_view(execution)
            await self._revalidate(tenant, control, plan)
            await self._approval(tenant, plan)
            now = await self._now(tenant)
            if execution is not None:
                if execution.lease_expires_at > now or execution.owner_id == payload.owner_id:
                    raise ComplianceConflict("execution owned or recovery requires new owner")
                execution.owner_id = payload.owner_id
                execution.generation += 1
                execution.lease_expires_at = now + dt.timedelta(seconds=payload.lease_seconds)
            else:
                execution = ComplianceExecution(
                    id=uuid.uuid7(),
                    organization_id=tenant.organization_id,
                    plan_id=plan.id,
                    owner_id=payload.owner_id,
                    generation=1,
                    state="CLAIMED",
                    lease_expires_at=now + dt.timedelta(seconds=payload.lease_seconds),
                    result={},
                )
                tenant.session.add(execution)
                if plan.request_id is not None:
                    request = await self._request(tenant, plan.request_id)
                    require_transition(request.state, "EXECUTING")
                    request.state = "EXECUTING"
            await self._event(tenant, execution.id, "execution", "CLAIMED")
            return self._execution_view(execution)

    @staticmethod
    def _execution_view(row: ComplianceExecution) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "state": row.state,
            "generation": row.generation,
            "owner_id": str(row.owner_id),
            "result": row.result,
        }

    async def execute(
        self, principal: Principal, identity: uuid.UUID, fence: ExecutionFence
    ) -> dict[str, Any]:
        async with self._transaction(principal, PermissionKey.COMPLIANCE_EXECUTE) as (
            tenant,
            control,
        ):
            plan = await self._plan(tenant, identity)
            execution = await tenant.session.scalar(
                select(ComplianceExecution).where(ComplianceExecution.plan_id == plan.id)
            )
            if (
                execution is None
                or execution.owner_id != fence.owner_id
                or execution.generation != fence.generation
            ):
                raise ComplianceConflict("execution owner/generation mismatch")
            if execution.state != "CLAIMED":
                return self._execution_view(execution)
            if execution.lease_expires_at <= await self._now(tenant):
                raise ComplianceConflict("execution lease expired")
            await self._revalidate(tenant, control, plan)
            await self._approval(tenant, plan)
            target = await self._profile.locked_target(tenant, plan.subject_id)
            if execution.lease_expires_at <= await self._now(tenant):
                raise ComplianceConflict("execution lease expired before effect")
            if plan.action == Action.ACCESS:
                result = self._profile.export(target)
            elif plan.action == Action.ANONYMIZE:
                result = self._profile.anonymize(target, plan.target_version)
            elif plan.action == Action.RESTRICT:
                result = self._profile.restrict(target, plan.target_version)
            else:
                raise ComplianceConflict("unregistered action")
            execution.state = "COMPLETED"
            execution.result = result
            execution.completed_at = await self._now(tenant)
            if plan.request_id is not None:
                request = await self._request(tenant, plan.request_id)
                request.result = result
                request.state = "PARTIAL" if request.incomplete_resources else "COMPLETED"
                await self._event(tenant, request.id, "subject_request", request.state)
            await self._event(tenant, execution.id, "execution", "COMPLETED")
            return self._execution_view(execution)
