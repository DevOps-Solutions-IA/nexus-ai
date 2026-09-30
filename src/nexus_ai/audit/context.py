"""Source-owned audit provenance; inherited request/task context is never authority."""

from __future__ import annotations

import asyncio
import contextvars
import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

from nexus_ai.audit.contracts import AuditActor
from nexus_ai.core.context import RequestContext, current_context
from nexus_ai.core.errors import PermissionDeniedError
from nexus_ai.domain.auth.entities import Principal
from nexus_ai.domain.auth.repository import RefreshSessionRepository
from nexus_ai.domain.auth.state import PrincipalStateValidator
from nexus_ai.infrastructure.database import Database
from nexus_ai.infrastructure.tenant_session import TenantSession


@dataclass(frozen=True, slots=True)
class ActorBinding:
    organization_id: UUID
    actor: AuditActor
    task: asyncio.Task[object] | None
    request: RequestContext | None


_actor: contextvars.ContextVar[ActorBinding | None] = contextvars.ContextVar(
    "nexus_audit_actor", default=None
)


def bind_verified_actor(organization_id: UUID, actor: AuditActor) -> None:
    _actor.set(ActorBinding(organization_id, actor, asyncio.current_task(), current_context()))


@contextmanager
def audit_request_scope() -> Iterator[None]:
    token = _actor.set(None)
    try:
        yield
    finally:
        _actor.reset(token)


@contextmanager
def actor_scope(organization_id: UUID, actor: AuditActor) -> Iterator[None]:
    token = _actor.set(
        ActorBinding(organization_id, actor, asyncio.current_task(), current_context())
    )
    try:
        yield
    finally:
        _actor.reset(token)


def require_actor(organization_id: UUID) -> AuditActor:
    binding = _actor.get()
    if (
        binding is None
        or binding.organization_id != organization_id
        or binding.task is not asyncio.current_task()
        or binding.request is not current_context()
    ):
        raise PermissionDeniedError("Trusted audit actor provenance is required.")
    return binding.actor


def source_actor(organization_id: UUID, *, service: str) -> AuditActor:
    """Explicit internal service identity; HTTP source operations require verified actors."""
    if _actor.get() is not None or current_context() is not None:
        return require_actor(organization_id)
    return AuditActor(kind="SERVICE", service=service)


async def principal_actor(
    database: Database, tenant: TenantSession, principal: Principal
) -> AuditActor:
    if (
        principal.organization_id != tenant.organization_id
        or principal.expires_at <= dt.datetime.now(dt.UTC)
    ):
        raise PermissionDeniedError("Audit principal is outside the trusted tenant scope.")
    await PrincipalStateValidator(database).require_valid_in(
        tenant, user_id=principal.user_id, session_id=principal.session_id
    )
    session = await RefreshSessionRepository(tenant).by_id(principal.session_id)
    if session is None or session.user_id != principal.user_id:
        raise PermissionDeniedError("Audit principal does not own its session.")
    if _actor.get() is not None:
        actor = require_actor(tenant.organization_id)
        if actor.kind == "AI_AGENT":
            if actor.initiating_user_id != principal.user_id:
                raise PermissionDeniedError("AI initiator does not match the authorized principal.")
            return actor
        if actor.kind == "HUMAN" and actor.user_id != principal.user_id:
            raise PermissionDeniedError("Audit principal does not match the request actor.")
    return AuditActor(kind="HUMAN", user_id=principal.user_id, session_id=principal.session_id)
