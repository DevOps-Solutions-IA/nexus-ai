"""The automatic Organization provisioner (NXS-ORG-001).

Three explicit, documented phases (ADR-0050):

* Phase 1 — idempotency claim: insert the PENDING request row (unique key hash). A
  conflict resolves into replay/conflict/in-progress/resume handling from durable
  state only.
* Phase 2 — tenant baseline: one transaction creates the Organization core record
  (PROVISIONING → ACTIVE), the owner membership and org_owner assignment (P03
  primitives), P05 settings, the baseline dashboard configuration and the
  ``organizations.provisioned`` outbox intent. Nothing becomes operational until
  every mandatory step commits. Deterministic business failures roll back to a
  savepoint and persist a terminal FAILED request row with a sanitized error code.
* Phase 3 — finalization: mark the request COMPLETED in its own transaction.

A crash between phases leaves either nothing (rollback — same key retries cleanly) or
a PENDING row plus a committed Organization — the resume path re-runs Phase 1,
recognizes the completed work by the unique slug and finalizes deterministically.
NATS is never touched synchronously: the outbox row is part of Phase 2's transaction
and the relay publishes later, so a JetStream outage cannot corrupt provisioning.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any
from uuid import UUID

from sqlalchemy import text

from nexus_ai.core.config import Settings
from nexus_ai.core.context import current_context
from nexus_ai.core.errors import (
    IdempotencyKeyConflictError,
    NotFoundError,
    OrganizationConflictError,
    PermissionDeniedError,
    ProvisioningFailedError,
    ProvisioningInProgressError,
    ProvisioningOwnerUnavailableError,
)
from nexus_ai.core.logging import get_logger
from nexus_ai.domain.auth.entities import UserStatus
from nexus_ai.domain.auth.rbac import PermissionKey, RoleKey
from nexus_ai.domain.auth.repository import MembershipRepository, RoleAssignmentRepository
from nexus_ai.domain.dashboard.generator import GENERATOR
from nexus_ai.domain.organizations.entities import Organization, OrganizationDraft
from nexus_ai.domain.organizations.repository import OrganizationRepository
from nexus_ai.domain.organizations.status import OrganizationStatus, assert_transition
from nexus_ai.domain.provisioning.entities import (
    OnboardingRequest,
    ProvisioningRequestStatus,
    ProvisioningResult,
    ProvisioningStatusView,
)
from nexus_ai.domain.provisioning.repository import (
    DashboardConfigurationRepository,
    OrganizationSettingsRepository,
    PlatformGrantRepository,
    ProvisioningRequestRepository,
    _KeyHashConflict,
)
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.infrastructure.database import Database
from nexus_ai.infrastructure.tenant_session import TenantSession

_CREATE_CAPABILITY = PermissionKey.ORGANIZATION_CREATE.value

_DETERMINISTIC_BUSINESS_ERRORS = (
    OrganizationConflictError,
    ProvisioningOwnerUnavailableError,
)


class OrganizationProvisioner:
    """Orchestrates Organization onboarding. DB-centric; no external calls."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        publisher: EventPublisher,
        *,
        pending_stale_seconds: int = 60,
    ) -> None:
        self._settings = settings
        self._db = database
        self._publisher = publisher
        #: A PENDING claim older than this is treated as an abandoned crash and
        #: resumed deterministically; a younger one is in-flight elsewhere.
        self._pending_stale_seconds = pending_stale_seconds
        self._log = get_logger("nexus_ai.provisioning")

    # -- public API --------------------------------------------------------------

    async def provision(
        self,
        request: OnboardingRequest,
        *,
        caller_user_id: UUID,
        caller_has_platform_grant: bool,
    ) -> ProvisioningResult:
        """Provision a new Organization for the onboarding request (idempotent)."""
        if not caller_has_platform_grant:
            raise PermissionDeniedError(
                "Creating an Organization requires the organization:create platform capability.",
                extensions={"permission": _CREATE_CAPABILITY},
            )
        owner_user_id = request.owner_user_id or caller_user_id
        # The initial owner must ALREADY exist and be ACTIVE (documented
        # precondition — P05 never creates users). Validated BEFORE the idempotency
        # claim so an unavailable owner is a deterministic business failure.
        from nexus_ai.domain.auth.repository import UserRepository

        async with self._db.transaction() as session:
            owner = await UserRepository(session).by_id(owner_user_id)
        if owner is None or owner.status is not UserStatus.ACTIVE:
            raise ProvisioningOwnerUnavailableError(
                "The initial owner must be an existing, ACTIVE user."
            )
        request_id = uuid.uuid7()
        key_hash = request.idempotency_key_hash()
        fingerprint = request.fingerprint()

        # --- Phase 1: idempotency claim (durable, unique) --------------------------
        try:
            async with self._db.transaction() as session:
                await ProvisioningRequestRepository(session).insert_pending(
                    request_id=request_id,
                    key_hash=key_hash,
                    fingerprint=fingerprint,
                    organization_key=request.organization_key,
                    owner_user_id=owner_user_id,
                    created_by_user_id=caller_user_id,
                    request_payload=json.loads(request.canonical_payload()),
                )
        except _KeyHashConflict:
            return await self._handle_existing_key(request, key_hash=key_hash)

        return await self._execute_baseline(
            request_id, request, owner_user_id=owner_user_id, caller_user_id=caller_user_id
        )

    async def _execute_baseline(
        self,
        request_id: uuid.UUID,
        request: OnboardingRequest,
        *,
        owner_user_id: UUID,
        caller_user_id: UUID,
    ) -> ProvisioningResult:
        # --- Phase 2: tenant baseline (one transaction, atomic) --------------------
        failure: Exception | None = None
        organization_id = uuid.uuid7()
        async with self._db.session() as session, session.begin():
            try:
                async with session.begin_nested():
                    # Bind the transaction-local tenant scope for the NEW
                    # Organization: the self-scoped policy checks ``id = org GUC``,
                    # so the core record can only carry its own fresh identity.
                    await session.execute(
                        text("SELECT set_config(:name, :value, true)"),
                        {
                            "name": self._settings.tenancy.context_setting_name,
                            "value": str(organization_id),
                        },
                    )
                    tenant = TenantSession(organization_id=organization_id, session=session)
                    organization = await self._create_organization(tenant, request)
                    await self._bind_owner(tenant, organization, owner_user_id)
                    await OrganizationSettingsRepository(tenant).insert(
                        settings_id=uuid.uuid7(),
                        organization_id=organization.id,
                        locale=request.locale,
                    )
                    dashboard = await self._provision_dashboard(tenant, organization)
                    await self._enqueue_provisioned_event(
                        session,
                        organization,
                        dashboard_revision=dashboard.revision,
                        created_by_user_id=caller_user_id,
                    )
                    # Operational only now — every mandatory step is in THIS
                    # transaction, so an ACTIVE Organization is always complete.
                    await self._activate(tenant, organization)
            except _DETERMINISTIC_BUSINESS_ERRORS as exc:
                # Roll back to the savepoint, then persist a terminal FAILED
                # request with a sanitized error code (the outer transaction is
                # healthy — the savepoint absorbed the failure).
                await ProvisioningRequestRepository(session).mark_failed(
                    request_id, error_code=exc.code
                )
                failure = exc
        if failure is not None:
            raise failure

        # --- Phase 3: finalization -------------------------------------------------
        async with self._db.transaction() as session:
            await ProvisioningRequestRepository(session).complete(
                request_id, organization_id=organization_id
            )
        await self._log.ainfo(
            "organization_provisioned",
            organization_id=str(organization_id),
            organization_key=request.organization_key,
            provisioning_request_id=str(request_id),
        )
        return ProvisioningResult(
            organization_id=organization_id,
            organization_key=request.organization_key,
            organization_status=OrganizationStatus.ACTIVE.value,
            provisioning_status=ProvisioningRequestStatus.COMPLETED,
            dashboard_revision=1,
        )

    # -- phase 2 steps ------------------------------------------------------------

    async def _create_organization(
        self, tenant: TenantSession, request: OnboardingRequest
    ) -> Organization:
        draft = OrganizationDraft(
            organization_key=request.organization_key,
            display_name=request.display_name,
            legal_name=request.legal_name,
            country_code=request.country_code,
            timezone=request.timezone,
        )
        # OrganizationRepository.insert maps a slug unique-violation to the stable
        # NXS_ORG_CONFLICT contract (P02 behavior, reused).
        return await OrganizationRepository(tenant).insert(
            draft, organization_id=tenant.organization_id
        )

    async def _bind_owner(
        self, tenant: TenantSession, organization: Organization, owner_user_id: UUID
    ) -> None:
        membership = await MembershipRepository(tenant).insert(
            membership_id=uuid.uuid7(),
            organization_id=organization.id,
            user_id=owner_user_id,
        )
        await self._log.ainfo(
            "provisioning_owner_bound",
            organization_id=str(organization.id),
            membership_id=str(membership.id),
        )
        role_id = await RoleAssignmentRepository(tenant).role_id_by_key(RoleKey.ORG_OWNER.value)
        if role_id is None:
            raise ProvisioningOwnerUnavailableError(
                "The org_owner role is missing from the platform catalog."
            )
        # The initial owner role is FIXED to org_owner: no onboarding field can select
        # a role, so arbitrary-role injection is structurally impossible.
        await RoleAssignmentRepository(tenant).assign(
            assignment_id=uuid.uuid7(),
            organization_id=organization.id,
            user_id=owner_user_id,
            role_id=role_id,
        )

    async def _provision_dashboard(self, tenant: TenantSession, organization: Organization) -> Any:
        generated = GENERATOR.generate(
            organization_id=organization.id,
            revision=1,
            generated_at=dt.datetime.now(dt.UTC),
        )
        return await DashboardConfigurationRepository(tenant).insert(
            config_id=uuid.uuid7(),
            organization_id=organization.id,
            schema_version=generated.schema_version,
            revision=generated.revision,
            configuration=generated.model_dump(mode="json"),
        )

    async def _activate(self, tenant: TenantSession, organization: Organization) -> None:
        assert_transition(OrganizationStatus.PROVISIONING, OrganizationStatus.ACTIVE)
        await OrganizationRepository(tenant).apply(
            expected_version=organization.version,
            changes={
                "status": OrganizationStatus.ACTIVE.value,
                "activated_at": dt.datetime.now(dt.UTC),
            },
        )

    async def _enqueue_provisioned_event(
        self,
        session: Any,
        organization: Organization,
        *,
        dashboard_revision: int,
        created_by_user_id: UUID,
    ) -> None:
        request_context = current_context()
        envelope = EventEnvelope.create(
            event_type="organizations.provisioned",
            event_version=1,
            aggregate_type="organization",
            aggregate_id=str(organization.id),
            producer=self._settings.service_name,
            organization_id=organization.id,
            correlation_id=(None if request_context is None else request_context.correlation_id),
            payload={
                "organization_id": str(organization.id),
                "organization_key": organization.organization_key,
                "created_by_user_id": str(created_by_user_id),
                "dashboard_schema_version": GENERATOR.current_version(),
                "dashboard_revision": dashboard_revision,
            },
        )
        # The outbox insert shares Phase 2's transaction: a failure to enqueue rolls
        # the WHOLE provisioning back (audit requirement).
        await self._publisher.enqueue(session, envelope)

    # -- idempotent replay / conflict / resume ------------------------------------

    async def _handle_existing_key(
        self, request: OnboardingRequest, *, key_hash: str
    ) -> ProvisioningResult:
        async with self._db.transaction() as session:
            existing = await ProvisioningRequestRepository(session).by_key_hash(key_hash)
        if existing is None:  # pragma: no cover - defensive: a conflict implies a row
            raise ProvisioningInProgressError("The idempotency claim is being finalized.")
        if existing.request_fingerprint != request.fingerprint():
            raise IdempotencyKeyConflictError(
                "This idempotency key was already used with a different request payload."
            )
        status = ProvisioningRequestStatus(existing.status)
        if status is ProvisioningRequestStatus.COMPLETED and existing.organization_id is not None:
            return ProvisioningResult(
                organization_id=existing.organization_id,
                organization_key=existing.organization_key,
                organization_status=OrganizationStatus.ACTIVE.value,
                provisioning_status=ProvisioningRequestStatus.COMPLETED,
                dashboard_revision=1,
            )
        if status is ProvisioningRequestStatus.FAILED:
            raise ProvisioningFailedError(
                "A previous provisioning attempt with this idempotency key failed "
                "terminally. Retry with a new idempotency key after correcting the input.",
                original_error_code=existing.error_code or "NXS_PROV_FAILED",
            )
        # PENDING: either still executing elsewhere, or a crash between Phase 2 and 3.
        return await self._resume_pending(existing)

    async def _resume_pending(self, existing: Any) -> ProvisioningResult:
        organization_key = existing.organization_key
        async with self._db.transaction() as session:
            by_slug = await ProvisioningRequestRepository(session).by_organization_key(
                organization_key
            )
        if by_slug is not None and by_slug.organization_id is not None:
            # Phase 2 committed before the crash: finalize deterministically.
            async with self._db.transaction() as session:
                await ProvisioningRequestRepository(session).complete(
                    by_slug.id, organization_id=by_slug.organization_id
                )
            return ProvisioningResult(
                organization_id=by_slug.organization_id,
                organization_key=by_slug.organization_key,
                organization_status=OrganizationStatus.ACTIVE.value,
                provisioning_status=ProvisioningRequestStatus.COMPLETED,
                dashboard_revision=1,
            )
        # No Organization yet: either the request is genuinely in-flight right now,
        # or a crash abandoned the claim. A stale claim resumes deterministically —
        # the unique slug keeps a concurrent re-run safe — a fresh one reports
        # in-progress (retryable).
        stale_before = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=self._pending_stale_seconds)
        if existing.updated_at <= stale_before:
            # Reconstruct the request from the VALIDATED canonical snapshot (the
            # ledger never stores caller-verbatim input) and re-run the safe P05
            # steps under the original claim. The unique slug keeps any concurrent
            # re-run safe.
            # The stored snapshot is the canonical payload WITHOUT the idempotency key
            # (the key is credential-like; only its hash is stored). Reconstruction
            # uses a deterministic placeholder that is never persisted or claimed.
            restored = OnboardingRequest.model_validate(
                {**existing.request_payload, "idempotency_key": "resume-00000000"}
            )
            if restored.fingerprint() != existing.request_fingerprint:
                # The stored snapshot disagrees with its own fingerprint: fail closed.
                raise ProvisioningFailedError(
                    "The stored provisioning request failed its integrity check.",
                    original_error_code="NXS_PROV_FAILED",
                )
            return await self._execute_baseline(
                existing.id,
                restored,
                owner_user_id=existing.owner_user_id,
                caller_user_id=existing.created_by_user_id,
            )
        raise ProvisioningInProgressError(
            "A provisioning request with this idempotency key is still executing. "
            "Retry shortly; the operation is idempotent."
        )

    # -- status projection --------------------------------------------------------

    async def status_for(self, organization_id: UUID) -> ProvisioningStatusView:
        async with self._db.transaction() as session:
            row = await ProvisioningRequestRepository(session).by_organization(organization_id)
        if row is None:
            raise NotFoundError("No provisioning record exists for this Organization.")
        return ProvisioningStatusView(
            organization_id=organization_id,
            organization_key=row.organization_key,
            status=ProvisioningRequestStatus(row.status),
            error_code=row.error_code,
            requested_at=row.created_at,
            completed_at=row.completed_at,
        )

    # -- platform grants (internal seam: tests and future admin surfaces) ---------

    async def grant_create_capability(
        self, *, user_id: UUID, granted_by: UUID | None = None
    ) -> None:
        async with self._db.transaction() as session:
            await PlatformGrantRepository(session).grant(
                user_id=user_id, capability=_CREATE_CAPABILITY, granted_by=granted_by
            )

    async def has_create_capability(self, user_id: UUID) -> bool:
        async with self._db.transaction() as session:
            return await PlatformGrantRepository(session).has_capability(
                user_id, _CREATE_CAPABILITY
            )
