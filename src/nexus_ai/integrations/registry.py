"""The Integration Registry (NXS-INT-001, ADR-0054).

Tenant-owned CRUD for integrations, their config-versioned operations and their inbound
webhook endpoints, plus the credential-management surface (which only ever writes
ciphertext via the vault seam). Every destination is SSRF-validated at CONFIGURATION time
here — not just at execution — and any change to a base URL, an auth profile, a
destination rule or an operation spec increments ``config_revision`` so an execution can
be attributed to the exact configuration it ran against.
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from nexus_ai.core.config import Settings
from nexus_ai.core.context import current_context
from nexus_ai.core.logging import get_logger
from nexus_ai.domain.integrations.repository import (
    IntegrationOperationRepository,
    IntegrationRepository,
    WebhookEndpointRepository,
)
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.infrastructure.database import Database
from nexus_ai.integrations.credentials import CredentialType, SecretMaterial, VaultClient
from nexus_ai.integrations.destination import DestinationPolicy
from nexus_ai.integrations.entities import (
    AuthProfile,
    AuthProfileType,
    CreateIntegrationRequest,
    GraphQLOperationSpec,
    Integration,
    IntegrationOperation,
    IntegrationStatus,
    OperationType,
    RestOperationSpec,
    SetOperationRequest,
    UpdateIntegrationRequest,
    WebhookEndpoint,
    WebhookSignatureScheme,
)
from nexus_ai.integrations.errors import (
    IntegrationConfigInvalidError,
    IntegrationDisabledError,
    IntegrationNotFoundError,
    IntegrationOperationNotFoundError,
)
from nexus_ai.integrations.graphql import validate_operation_spec
from nexus_ai.integrations.openapi import OpenApiImportResult, OpenApiIngestor
from nexus_ai.integrations.rest import build_url
from nexus_ai.integrations.webhooks import build_webhook_token

_ACTIVATABLE = {IntegrationStatus.ACTIVE, IntegrationStatus.DISABLED}


class IntegrationRegistry:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        publisher: EventPublisher,
        vault: VaultClient,
        policy: DestinationPolicy,
    ) -> None:
        self._settings = settings
        self._db = database
        self._publisher = publisher
        self._vault = vault
        self._policy = policy
        self._ingestor = OpenApiIngestor(settings.integrations, policy)
        self._log = get_logger("nexus_ai.integrations.registry")

    # -- integrations ----------------------------------------------------------

    async def create(self, organization_id: UUID, request: CreateIntegrationRequest) -> Integration:
        self._policy.validate_url(request.base_url, rule=request.destination_rule)
        await self._require_credential(organization_id, request.auth_profile)
        async with self._db.tenant_transaction(organization_id) as tenant:
            integration = await IntegrationRepository(tenant).insert(
                integration_id=uuid.uuid7(),
                slug=request.slug,
                name=request.name,
                description=request.description,
                integration_type=request.integration_type,
                base_url=request.base_url.rstrip("/"),
                auth_profile=request.auth_profile,
                destination_rule=request.destination_rule,
            )
            await self._emit(
                tenant.session,
                organization_id,
                "integrations.created",
                integration.id,
                {
                    "integration_id": str(integration.id),
                    "slug": integration.slug,
                    "integration_type": integration.integration_type.value,
                },
            )
            return integration

    async def get(self, organization_id: UUID, integration_id: UUID) -> Integration:
        async with self._db.tenant_transaction(organization_id) as tenant:
            integration = await IntegrationRepository(tenant).by_id(integration_id)
        if integration is None:
            raise IntegrationNotFoundError("no such integration in this Organization")
        return integration

    async def list_integrations(
        self, organization_id: UUID, *, limit: int, after_id: UUID | None
    ) -> list[Integration]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            rows = await IntegrationRepository(tenant).list_all(limit=limit, after_id=after_id)
        return rows[:limit]

    async def update(
        self, organization_id: UUID, integration_id: UUID, request: UpdateIntegrationRequest
    ) -> Integration:
        current = await self.get(organization_id, integration_id)
        changes: dict[str, Any] = {}
        bump = False
        if request.name is not None:
            changes["name"] = request.name
        if request.description is not None:
            changes["description"] = request.description
        new_rule = request.destination_rule or current.destination_rule
        if request.base_url is not None and request.base_url.rstrip("/") != current.base_url:
            self._policy.validate_url(request.base_url, rule=new_rule)
            changes["base_url"] = request.base_url.rstrip("/")
            bump = True
        if request.destination_rule is not None:
            self._policy.validate_url(changes.get("base_url", current.base_url), rule=new_rule)
            changes["destination_rule"] = new_rule.model_dump(mode="json")
            bump = True
        if request.auth_profile is not None:
            await self._require_credential(organization_id, request.auth_profile)
            changes["auth_profile"] = request.auth_profile.model_dump(mode="json")
            bump = True
        if not changes:
            return current
        async with self._db.tenant_transaction(organization_id) as tenant:
            updated = await IntegrationRepository(tenant).apply(
                integration_id, changes=changes, bump_revision=bump
            )
            if updated is None:  # pragma: no cover - get() already proved existence
                raise IntegrationNotFoundError("no such integration in this Organization")
            if bump:
                await self._emit(
                    tenant.session,
                    organization_id,
                    "integrations.updated",
                    integration_id,
                    {
                        "integration_id": str(integration_id),
                        "config_revision": updated.config_revision,
                    },
                )
            return updated

    async def set_status(
        self, organization_id: UUID, integration_id: UUID, status: IntegrationStatus
    ) -> Integration:
        if status not in _ACTIVATABLE:
            raise IntegrationConfigInvalidError("status may only be set to ACTIVE or DISABLED")
        await self.get(organization_id, integration_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            updated = await IntegrationRepository(tenant).apply(
                integration_id, changes={"status": status.value}, bump_revision=False
            )
            assert updated is not None  # noqa: S101 - get() proved existence
            if status is IntegrationStatus.DISABLED:
                await self._emit(
                    tenant.session,
                    organization_id,
                    "integrations.disabled",
                    integration_id,
                    {"integration_id": str(integration_id), "status": status.value},
                )
            return updated

    # -- operations ----------------------------------------------------------

    async def set_operation(
        self, organization_id: UUID, integration_id: UUID, request: SetOperationRequest
    ) -> IntegrationOperation:
        integration = await self.get(organization_id, integration_id)
        spec = request.spec
        if isinstance(spec, GraphQLOperationSpec):
            validate_operation_spec(
                spec, max_depth_limit=self._settings.integrations.openapi_max_depth
            )
            operation_type = OperationType.GRAPHQL
        else:
            self._validate_rest_destination(integration.base_url, spec, integration)
            operation_type = OperationType.REST
        async with self._db.tenant_transaction(organization_id) as tenant:
            operation = await IntegrationOperationRepository(tenant).upsert(
                integration_id=integration_id,
                operation_key=request.operation_key,
                operation_type=operation_type,
                spec_json=spec.model_dump(mode="json"),
                config_revision=integration.config_revision,
            )
            await self._emit(
                tenant.session,
                organization_id,
                "integrations.operation.created",
                integration_id,
                {
                    "integration_id": str(integration_id),
                    "operation_key": operation.operation_key,
                    "operation_type": operation_type.value,
                    "config_revision": integration.config_revision,
                },
            )
            return operation

    async def list_operations(
        self, organization_id: UUID, integration_id: UUID
    ) -> list[IntegrationOperation]:
        await self.get(organization_id, integration_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await IntegrationOperationRepository(tenant).list_for(integration_id)

    async def remove_operation(
        self, organization_id: UUID, integration_id: UUID, operation_key: str
    ) -> None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            removed = await IntegrationOperationRepository(tenant).delete_one(
                integration_id, operation_key
            )
        if not removed:
            raise IntegrationOperationNotFoundError("no such operation on this integration")

    async def resolve_for_execution(
        self, organization_id: UUID, integration_id: UUID, operation_key: str
    ) -> tuple[Integration, IntegrationOperation]:
        async with self._db.tenant_transaction(organization_id) as tenant:
            integration = await IntegrationRepository(tenant).by_id(integration_id)
            if integration is None:
                raise IntegrationNotFoundError("no such integration in this Organization")
            operation = await IntegrationOperationRepository(tenant).get(
                integration_id, operation_key
            )
        if integration.status is not IntegrationStatus.ACTIVE:
            raise IntegrationDisabledError(
                "the integration is not ACTIVE", extensions={"status": integration.status.value}
            )
        if operation is None:
            raise IntegrationOperationNotFoundError("no such operation on this integration")
        return integration, operation

    # -- webhooks ----------------------------------------------------------

    async def register_webhook(
        self,
        organization_id: UUID,
        integration_id: UUID,
        *,
        slug: str,
        event_type: str,
        signature_scheme: WebhookSignatureScheme,
        signature_header: str | None,
        timestamp_header: str | None,
        tolerance_seconds: int,
        credential_ref: str | None,
    ) -> WebhookEndpoint:
        await self.get(organization_id, integration_id)
        if signature_scheme is WebhookSignatureScheme.HMAC_SHA256:
            if not signature_header or not credential_ref:
                raise IntegrationConfigInvalidError(
                    "an HMAC webhook requires a signature_header and a credential_ref"
                )
            await self._vault.get_secret(organization_id, credential_ref)
        public_token = build_webhook_token(organization_id)
        async with self._db.tenant_transaction(organization_id) as tenant:
            return await WebhookEndpointRepository(tenant).insert(
                endpoint_id=uuid.uuid7(),
                integration_id=integration_id,
                slug=slug,
                public_token=public_token,
                event_type=event_type,
                signature_scheme=signature_scheme,
                signature_header=signature_header,
                timestamp_header=timestamp_header,
                tolerance_seconds=tolerance_seconds,
                max_body_bytes=self._settings.integrations.webhook_max_body_bytes,
                credential_ref=credential_ref,
            )

    # -- credentials -----------------------------------------------------

    async def store_credential(
        self,
        organization_id: UUID,
        *,
        credential_ref: str,
        credential_type: CredentialType,
        fields: dict[str, str],
    ) -> None:
        material = SecretMaterial(credential_type, dict(fields))
        await self._vault.store_secret(organization_id, credential_ref, material)

    async def delete_credential(self, organization_id: UUID, credential_ref: str) -> None:
        await self._vault.delete_secret(organization_id, credential_ref)

    # -- openapi -------------------------------------------------------

    def import_openapi(
        self, organization_id: UUID, raw: bytes, *, destination_rule: Any = None
    ) -> OpenApiImportResult:
        return self._ingestor.ingest(raw, destination_rule=destination_rule)

    # -- helpers -----------------------------------------------------

    async def _require_credential(self, organization_id: UUID, profile: AuthProfile) -> None:
        if profile.profile_type is AuthProfileType.NONE or profile.credential_ref is None:
            return
        if not await self._vault.has_secret(organization_id, profile.credential_ref):
            raise IntegrationConfigInvalidError(
                "the auth profile references a credential that has not been stored"
            )

    def _validate_rest_destination(
        self, base_url: str, spec: RestOperationSpec, integration: Integration
    ) -> None:
        sample_path = spec.path
        for name in spec.path_params:
            sample_path = sample_path.replace(f"{{{name}}}", "x")
        candidate = build_url(base_url, sample_path, [])
        self._policy.validate_url(candidate, rule=integration.destination_rule)

    async def _emit(
        self,
        session: Any,
        organization_id: UUID,
        event_type: str,
        integration_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        ctx = current_context()
        envelope = EventEnvelope.create(
            event_type=event_type,
            event_version=1,
            aggregate_type="integration",
            aggregate_id=str(integration_id),
            producer=self._settings.service_name,
            organization_id=organization_id,
            correlation_id=None if ctx is None else ctx.correlation_id,
            payload=payload,
        )
        await self._publisher.enqueue(session, envelope)
