"""Bounded, live-authorized tenant audit reads and integrity verification."""

import datetime as dt
from dataclasses import asdict
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from nexus_ai.api.auth_deps import LivePrincipalDep
from nexus_ai.api.dependencies import get_resources
from nexus_ai.audit.contracts import ACTIONS, TARGETS
from nexus_ai.audit.repository import AuditRepository
from nexus_ai.core.errors import NotFoundError
from nexus_ai.domain.auth.rbac import PermissionKey

audit_router = APIRouter(prefix="/audit", tags=["audit"])


class AuditQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=50, ge=1, le=100)
    after: int = Field(default=0, ge=0, le=2**63 - 1)
    high_water: int | None = Field(default=None, ge=0, le=2**63 - 1)
    actor_id: UUID | None = None
    action: str | None = Field(default=None, max_length=80)
    producer: str | None = Field(default=None, max_length=32)
    target_type: str | None = Field(default=None, max_length=32)
    target_id: UUID | None = None
    source_id: UUID | None = None
    correlation_id: UUID | None = None
    outcome: Literal["SUCCESS", "FAILED", "DENIED", "AMBIGUOUS"] | None = None
    since: dt.datetime | None = None
    until: dt.datetime | None = None

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if self.after and self.high_water is None:
            raise ValueError("continuation requires high_water")
        if self.high_water is not None and self.after > self.high_water:
            raise ValueError("invalid snapshot cursor")
        if self.producer is not None and self.producer not in ACTIONS:
            raise ValueError("unknown producer")
        if self.action is not None and not any(
            self.action in actions for actions in ACTIONS.values()
        ):
            raise ValueError("unknown action")
        if self.target_type is not None and self.target_type not in TARGETS:
            raise ValueError("unknown target type")
        for timestamp in (self.since, self.until):
            if timestamp is not None and timestamp.tzinfo is None:
                raise ValueError("timestamps require timezone")
        if (self.since is None) != (self.until is None):
            raise ValueError("time window requires both bounds")
        if (
            self.since is not None
            and self.until is not None
            and not dt.timedelta(0) <= self.until - self.since <= dt.timedelta(days=366)
        ):
            raise ValueError("time window exceeds bounds")
        return self


@audit_router.get("/records")
async def records(
    request: Request,
    principal: LivePrincipalDep,
    query: Annotated[AuditQuery, Query()],
) -> dict[str, Any]:
    resources = get_resources(request)
    async with resources.database.tenant_transaction(principal.organization_id) as tenant:
        await resources.authorizer.require(principal, PermissionKey.AUDIT_READ, tenant=tenant)
        filters = {
            key: str(value)
            for key, value in query.model_dump(exclude_none=True).items()
            if key not in {"limit", "after", "high_water", "since", "until"}
        }
        return await AuditRepository(tenant).page(
            limit=query.limit,
            after=query.after,
            high_water=query.high_water,
            filters=filters,
            since=query.since,
            until=query.until,
        )


@audit_router.get("/records/{identity}")
async def record(identity: UUID, request: Request, principal: LivePrincipalDep) -> dict[str, Any]:
    resources = get_resources(request)
    async with resources.database.tenant_transaction(principal.organization_id) as tenant:
        await resources.authorizer.require(principal, PermissionKey.AUDIT_READ, tenant=tenant)
        row = await AuditRepository(tenant).get(identity)
        if row is None:
            raise NotFoundError("Audit record not found.")
        return row.fact | {"digest": row.digest}


@audit_router.get("/verify")
async def verify(
    request: Request,
    principal: LivePrincipalDep,
    start: Annotated[int, Query(ge=1, le=2**63 - 1001)] = 1,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> dict[str, Any]:
    resources = get_resources(request)
    async with resources.database.tenant_transaction(principal.organization_id) as tenant:
        await resources.authorizer.require(principal, PermissionKey.AUDIT_VERIFY, tenant=tenant)
        return asdict(await AuditRepository(tenant).verify(start=start, limit=limit))
