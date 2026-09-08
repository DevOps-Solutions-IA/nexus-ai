"""Integration Hub persistence (NXS-INT-001).

Registry repositories are tenant-scoped by a :class:`TenantSession`. The secret store and
the idempotency store take the :class:`Database` and open their own tenant transactions —
they are used both from the API (credential management) and from the execution path.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, delete, select, update
from sqlalchemy.exc import IntegrityError

from nexus_ai.domain.integrations.models import (
    IntegrationExecutionRecord,
    IntegrationIdempotencyRecord,
    IntegrationOperationRecord,
    IntegrationRecord,
    IntegrationSecretRecord,
    WebhookEndpointRecord,
    WebhookReceiptRecord,
)
from nexus_ai.infrastructure.database import Database
from nexus_ai.infrastructure.tenant_session import TenantSession
from nexus_ai.integrations.credentials import (
    CredentialType,
    EncryptedSecret,
)
from nexus_ai.integrations.entities import (
    AuthProfile,
    DestinationRule,
    Integration,
    IntegrationOperation,
    IntegrationStatus,
    IntegrationType,
    OperationType,
    WebhookEndpoint,
    WebhookSignatureScheme,
)
from nexus_ai.integrations.errors import IntegrationConflictError
from nexus_ai.integrations.idempotency import (
    ClaimOutcome,
    IdempotencyRecord,
    IdempotencyStatus,
)


def _to_integration(row: IntegrationRecord) -> Integration:
    return Integration(
        id=row.id,
        organization_id=row.organization_id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        integration_type=IntegrationType(row.integration_type),
        status=IntegrationStatus(row.status),
        base_url=row.base_url,
        config_revision=row.config_revision,
        auth_profile=AuthProfile.model_validate(row.auth_profile),
        destination_rule=DestinationRule.model_validate(row.destination_rule),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_operation(row: IntegrationOperationRecord) -> IntegrationOperation:
    return IntegrationOperation.model_validate(
        {
            "id": row.id,
            "organization_id": row.organization_id,
            "integration_id": row.integration_id,
            "operation_key": row.operation_key,
            "operation_type": row.operation_type,
            "spec": row.spec,
            "config_revision": row.config_revision,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )


def _to_endpoint(row: WebhookEndpointRecord) -> WebhookEndpoint:
    return WebhookEndpoint(
        id=row.id,
        organization_id=row.organization_id,
        integration_id=row.integration_id,
        slug=row.slug,
        public_token=row.public_token,
        event_type=row.event_type,
        signature_scheme=WebhookSignatureScheme(row.signature_scheme),
        signature_header=row.signature_header,
        timestamp_header=row.timestamp_header,
        tolerance_seconds=row.tolerance_seconds,
        max_body_bytes=row.max_body_bytes,
        credential_ref=row.credential_ref,
        created_at=row.created_at,
    )


class IntegrationRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def by_id(self, integration_id: UUID) -> Integration | None:
        row = await self._session.get(IntegrationRecord, integration_id)
        return None if row is None else _to_integration(row)

    async def by_slug(self, slug: str) -> Integration | None:
        row = (
            (
                await self._session.execute(
                    select(IntegrationRecord).where(
                        IntegrationRecord.organization_id == self._tenant.organization_id,
                        IntegrationRecord.slug == slug,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_integration(row)

    async def list_all(self, *, limit: int, after_id: UUID | None) -> list[Integration]:
        query = (
            select(IntegrationRecord)
            .where(IntegrationRecord.organization_id == self._tenant.organization_id)
            .order_by(IntegrationRecord.created_at.desc(), IntegrationRecord.id.desc())
            .limit(limit + 1)
        )
        if after_id is not None:
            query = query.where(IntegrationRecord.id < after_id)
        rows = (await self._session.execute(query)).scalars()
        return [_to_integration(row) for row in rows]

    async def insert(
        self,
        *,
        integration_id: UUID,
        slug: str,
        name: str,
        description: str | None,
        integration_type: IntegrationType,
        base_url: str,
        auth_profile: AuthProfile,
        destination_rule: DestinationRule,
    ) -> Integration:
        now = dt.datetime.now(dt.UTC)
        record = IntegrationRecord(
            id=integration_id,
            organization_id=self._tenant.organization_id,
            slug=slug,
            name=name,
            description=description,
            integration_type=integration_type.value,
            status=IntegrationStatus.DRAFT.value,
            base_url=base_url,
            config_revision=1,
            auth_profile=auth_profile.model_dump(mode="json"),
            destination_rule=destination_rule.model_dump(mode="json"),
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if "uq_integrations_org_slug" in str(exc.orig):
                raise IntegrationConflictError(
                    "an integration with that slug already exists", cause=exc
                ) from exc
            raise
        await self._session.refresh(record)
        return _to_integration(record)

    async def apply(
        self, integration_id: UUID, *, changes: dict[str, Any], bump_revision: bool
    ) -> Integration | None:
        values = dict(changes)
        values["updated_at"] = dt.datetime.now(dt.UTC)
        if bump_revision:
            values["config_revision"] = IntegrationRecord.config_revision + 1
        result = await self._session.execute(
            update(IntegrationRecord)
            .where(
                IntegrationRecord.id == integration_id,
                IntegrationRecord.organization_id == self._tenant.organization_id,
            )
            .values(**values)
            .returning(IntegrationRecord)
        )
        updated = result.scalars().one_or_none()
        return None if updated is None else _to_integration(updated)


class IntegrationOperationRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def get(self, integration_id: UUID, operation_key: str) -> IntegrationOperation | None:
        row = (
            (
                await self._session.execute(
                    select(IntegrationOperationRecord).where(
                        IntegrationOperationRecord.organization_id == self._tenant.organization_id,
                        IntegrationOperationRecord.integration_id == integration_id,
                        IntegrationOperationRecord.operation_key == operation_key,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_operation(row)

    async def list_for(self, integration_id: UUID) -> list[IntegrationOperation]:
        rows = (
            await self._session.execute(
                select(IntegrationOperationRecord)
                .where(
                    IntegrationOperationRecord.organization_id == self._tenant.organization_id,
                    IntegrationOperationRecord.integration_id == integration_id,
                )
                .order_by(IntegrationOperationRecord.operation_key)
            )
        ).scalars()
        return [_to_operation(row) for row in rows]

    async def upsert(
        self,
        *,
        integration_id: UUID,
        operation_key: str,
        operation_type: OperationType,
        spec_json: dict[str, Any],
        config_revision: int,
    ) -> IntegrationOperation:
        now = dt.datetime.now(dt.UTC)
        existing = (
            (
                await self._session.execute(
                    select(IntegrationOperationRecord).where(
                        IntegrationOperationRecord.organization_id == self._tenant.organization_id,
                        IntegrationOperationRecord.integration_id == integration_id,
                        IntegrationOperationRecord.operation_key == operation_key,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        if existing is not None:
            existing.operation_type = operation_type.value
            existing.spec = spec_json
            existing.config_revision = config_revision
            existing.updated_at = now
            await self._session.flush()
            await self._session.refresh(existing)
            return _to_operation(existing)
        record = IntegrationOperationRecord(
            id=uuid.uuid7(),
            organization_id=self._tenant.organization_id,
            integration_id=integration_id,
            operation_key=operation_key,
            operation_type=operation_type.value,
            spec=spec_json,
            config_revision=config_revision,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)
        return _to_operation(record)

    async def delete_one(self, integration_id: UUID, operation_key: str) -> bool:
        result = await self._session.execute(
            delete(IntegrationOperationRecord).where(
                IntegrationOperationRecord.organization_id == self._tenant.organization_id,
                IntegrationOperationRecord.integration_id == integration_id,
                IntegrationOperationRecord.operation_key == operation_key,
            )
        )
        return bool(cast("CursorResult[Any]", result).rowcount)


class WebhookEndpointRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def by_slug(self, slug: str) -> WebhookEndpoint | None:
        row = (
            (
                await self._session.execute(
                    select(WebhookEndpointRecord).where(
                        WebhookEndpointRecord.organization_id == self._tenant.organization_id,
                        WebhookEndpointRecord.slug == slug,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_endpoint(row)

    async def by_public_token(self, public_token: str) -> WebhookEndpoint | None:
        row = (
            (
                await self._session.execute(
                    select(WebhookEndpointRecord).where(
                        WebhookEndpointRecord.organization_id == self._tenant.organization_id,
                        WebhookEndpointRecord.public_token == public_token,
                    )
                )
            )
            .scalars()
            .one_or_none()
        )
        return None if row is None else _to_endpoint(row)

    async def list_for(self, integration_id: UUID) -> list[WebhookEndpoint]:
        rows = (
            await self._session.execute(
                select(WebhookEndpointRecord)
                .where(
                    WebhookEndpointRecord.organization_id == self._tenant.organization_id,
                    WebhookEndpointRecord.integration_id == integration_id,
                )
                .order_by(WebhookEndpointRecord.slug)
            )
        ).scalars()
        return [_to_endpoint(row) for row in rows]

    async def insert(
        self,
        *,
        endpoint_id: UUID,
        integration_id: UUID,
        slug: str,
        public_token: str,
        event_type: str,
        signature_scheme: WebhookSignatureScheme,
        signature_header: str | None,
        timestamp_header: str | None,
        tolerance_seconds: int,
        max_body_bytes: int,
        credential_ref: str | None,
    ) -> WebhookEndpoint:
        now = dt.datetime.now(dt.UTC)
        record = WebhookEndpointRecord(
            id=endpoint_id,
            organization_id=self._tenant.organization_id,
            integration_id=integration_id,
            slug=slug,
            public_token=public_token,
            event_type=event_type,
            signature_scheme=signature_scheme.value,
            signature_header=signature_header,
            timestamp_header=timestamp_header,
            tolerance_seconds=tolerance_seconds,
            max_body_bytes=max_body_bytes,
            credential_ref=credential_ref,
            created_at=now,
            updated_at=now,
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            if "uq_webhook_endpoints_org_slug" in str(exc.orig):
                raise IntegrationConflictError(
                    "a webhook endpoint with that slug already exists", cause=exc
                ) from exc
            raise
        await self._session.refresh(record)
        return _to_endpoint(record)


class WebhookReceiptRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def claim(self, *, endpoint_id: UUID, external_id: str) -> bool:
        """Insert a RECEIVED receipt. Returns False if the external id is a replay."""
        record = WebhookReceiptRecord(
            id=uuid.uuid7(),
            organization_id=self._tenant.organization_id,
            webhook_endpoint_id=endpoint_id,
            external_id=external_id,
            status="RECEIVED",
            received_at=dt.datetime.now(dt.UTC),
            created_at=dt.datetime.now(dt.UTC),
        )
        self._session.add(record)
        try:
            await self._session.flush()
        except IntegrityError:
            return False
        return True

    async def mark(self, *, endpoint_id: UUID, external_id: str, status: str) -> None:
        await self._session.execute(
            update(WebhookReceiptRecord)
            .where(
                WebhookReceiptRecord.organization_id == self._tenant.organization_id,
                WebhookReceiptRecord.webhook_endpoint_id == endpoint_id,
                WebhookReceiptRecord.external_id == external_id,
            )
            .values(status=status)
        )


class IntegrationExecutionRepository:
    def __init__(self, tenant: TenantSession) -> None:
        self._tenant = tenant
        self._session = tenant.session

    async def record(
        self,
        *,
        integration_id: UUID,
        operation_key: str,
        config_revision: int,
        result_class: str,
        ok: bool,
        status_code: int,
        upstream_status: int | None,
        error_code: str | None,
        retry_count: int,
        duration_ms: int,
        correlation_id: str | None,
        idempotency_key: str | None,
    ) -> None:
        self._session.add(
            IntegrationExecutionRecord(
                id=uuid.uuid7(),
                organization_id=self._tenant.organization_id,
                integration_id=integration_id,
                operation_key=operation_key,
                config_revision=config_revision,
                result_class=result_class,
                ok=ok,
                status_code=status_code,
                upstream_status=upstream_status,
                error_code=error_code,
                retry_count=retry_count,
                duration_ms=duration_ms,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                created_at=dt.datetime.now(dt.UTC),
            )
        )
        await self._session.flush()


class IntegrationSecretStore:
    """Encrypted-at-rest ciphertext persistence — satisfies
    :class:`~nexus_ai.integrations.credentials.EncryptedSecretStore`."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def get(self, organization_id: UUID, ref: str) -> EncryptedSecret | None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            row = (
                (
                    await tenant.session.execute(
                        select(IntegrationSecretRecord).where(
                            IntegrationSecretRecord.organization_id == organization_id,
                            IntegrationSecretRecord.credential_ref == ref,
                        )
                    )
                )
                .scalars()
                .one_or_none()
            )
            if row is None:
                return None
            return EncryptedSecret(CredentialType(row.credential_type), row.ciphertext)

    async def put(self, organization_id: UUID, ref: str, secret: EncryptedSecret) -> None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            now = dt.datetime.now(dt.UTC)
            existing = (
                (
                    await tenant.session.execute(
                        select(IntegrationSecretRecord).where(
                            IntegrationSecretRecord.organization_id == organization_id,
                            IntegrationSecretRecord.credential_ref == ref,
                        )
                    )
                )
                .scalars()
                .one_or_none()
            )
            if existing is not None:
                existing.credential_type = secret.credential_type.value
                existing.ciphertext = secret.ciphertext
                existing.updated_at = now
            else:
                tenant.session.add(
                    IntegrationSecretRecord(
                        id=uuid.uuid7(),
                        organization_id=organization_id,
                        credential_ref=ref,
                        credential_type=secret.credential_type.value,
                        ciphertext=secret.ciphertext,
                        created_at=now,
                        updated_at=now,
                    )
                )
            await tenant.session.flush()

    async def delete(self, organization_id: UUID, ref: str) -> bool:
        async with self._db.tenant_transaction(organization_id) as tenant:
            result = await tenant.session.execute(
                delete(IntegrationSecretRecord).where(
                    IntegrationSecretRecord.organization_id == organization_id,
                    IntegrationSecretRecord.credential_ref == ref,
                )
            )
            return bool(cast("CursorResult[Any]", result).rowcount)


class IntegrationIdempotencyRepository:
    """Satisfies :class:`~nexus_ai.integrations.idempotency.IdempotencyStore`. Each method
    is its own tenant transaction (the P06 race-recovery pattern applies to ``claim``)."""

    def __init__(self, database: Database) -> None:
        self._db = database

    async def claim(
        self,
        *,
        organization_id: UUID,
        integration_id: UUID,
        operation_key: str,
        idempotency_key: str,
        request_fingerprint: str,
        expires_at: dt.datetime,
    ) -> ClaimOutcome:
        now = dt.datetime.now(dt.UTC)
        try:
            async with self._db.tenant_transaction(organization_id) as tenant:
                tenant.session.add(
                    IntegrationIdempotencyRecord(
                        id=uuid.uuid7(),
                        organization_id=organization_id,
                        integration_id=integration_id,
                        operation_key=operation_key,
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                        status=IdempotencyStatus.PENDING.value,
                        result_json=None,
                        error_code=None,
                        created_at=now,
                        updated_at=now,
                        expires_at=expires_at,
                    )
                )
                await tenant.session.flush()
                return ClaimOutcome(is_owner=True)
        except IntegrityError:
            pass
        existing = await self.get(
            organization_id=organization_id,
            integration_id=integration_id,
            operation_key=operation_key,
            idempotency_key=idempotency_key,
        )
        return ClaimOutcome(is_owner=False, existing=existing)

    async def finalize(
        self,
        *,
        organization_id: UUID,
        integration_id: UUID,
        operation_key: str,
        idempotency_key: str,
        status: IdempotencyStatus,
        result_json: dict[str, Any] | None,
        error_code: str | None,
    ) -> None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            await tenant.session.execute(
                update(IntegrationIdempotencyRecord)
                .where(
                    IntegrationIdempotencyRecord.organization_id == organization_id,
                    IntegrationIdempotencyRecord.integration_id == integration_id,
                    IntegrationIdempotencyRecord.operation_key == operation_key,
                    IntegrationIdempotencyRecord.idempotency_key == idempotency_key,
                )
                .values(
                    status=status.value,
                    result_json=result_json,
                    error_code=error_code,
                    updated_at=dt.datetime.now(dt.UTC),
                )
            )

    async def get(
        self,
        *,
        organization_id: UUID,
        integration_id: UUID,
        operation_key: str,
        idempotency_key: str,
    ) -> IdempotencyRecord | None:
        async with self._db.tenant_transaction(organization_id) as tenant:
            row = (
                (
                    await tenant.session.execute(
                        select(IntegrationIdempotencyRecord).where(
                            IntegrationIdempotencyRecord.organization_id == organization_id,
                            IntegrationIdempotencyRecord.integration_id == integration_id,
                            IntegrationIdempotencyRecord.operation_key == operation_key,
                            IntegrationIdempotencyRecord.idempotency_key == idempotency_key,
                        )
                    )
                )
                .scalars()
                .one_or_none()
            )
            if row is None:
                return None
            return IdempotencyRecord(
                idempotency_key=row.idempotency_key,
                request_fingerprint=row.request_fingerprint,
                status=IdempotencyStatus(row.status),
                result_json=row.result_json,
                error_code=row.error_code,
                updated_at=row.updated_at,
            )
