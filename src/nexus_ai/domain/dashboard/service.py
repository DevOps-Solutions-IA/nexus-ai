"""Dashboard schema service (NXS-DASH-001).

Serves the canonical persisted schema for the bound Organization: it MUST revalidate
against the allow-listed registry (unknown widgets, unregistered data sources,
permission tampering and unsupported versions fail closed) and is then projected to
exactly what the viewer's permissions allow.
"""

from __future__ import annotations

from pydantic import ValidationError as PydanticValidationError

from nexus_ai.core.errors import DashboardSchemaError, UnsupportedDashboardVersionError
from nexus_ai.domain.dashboard.registry import validate_generated_schema
from nexus_ai.domain.dashboard.schema import DASHBOARD_SCHEMA_VERSION, DashboardSchema
from nexus_ai.domain.provisioning.repository import DashboardConfigurationRepository
from nexus_ai.infrastructure.tenant_session import TenantSession


class DashboardSchemaService:
    async def for_organization(
        self, tenant: TenantSession, *, viewer_permissions: set[str]
    ) -> DashboardSchema:
        row = await DashboardConfigurationRepository(tenant).latest()
        if row is None:
            from nexus_ai.core.errors import NotFoundError

            raise NotFoundError("No dashboard configuration exists for this Organization.")
        if row.schema_version != DASHBOARD_SCHEMA_VERSION:
            raise UnsupportedDashboardVersionError(
                "the stored dashboard schema version is not supported",
                extensions={
                    "stored_version": row.schema_version,
                    "supported_version": DASHBOARD_SCHEMA_VERSION,
                },
            )
        try:
            schema = DashboardSchema.model_validate(row.configuration)
        except PydanticValidationError as exc:
            raise DashboardSchemaError(
                "The stored dashboard configuration is malformed.",
                extensions={"errors": exc.error_count()},
            ) from exc
        validate_generated_schema(schema)
        return schema.permission_filtered(viewer_permissions)
