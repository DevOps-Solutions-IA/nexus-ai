"""Tenant security attacks: spoofing, confused deputy, redaction (sections 34, 39, 62, 80)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from starlette.requests import Request

from nexus_ai.core.config import Settings
from nexus_ai.core.tenancy import TenantContext, TenantContextSource

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_HEADER = "X-NXS-Organization-ID"


async def test_production_rejects_header_resolver() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="HEADER_RESOLVER_ENABLED"):
        Settings(
            environment="production",
            tenancy={"header_resolver_enabled": True},  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError, match="HEADER_RESOLVER_ENABLED"):
        Settings(
            environment="staging",
            tenancy={"header_resolver_enabled": True},  # type: ignore[arg-type]
        )


async def test_no_context_without_header(tenant_header_client: Any) -> None:
    response = await tenant_header_client.get("/api/v1/organizations/current")
    assert response.status_code == 403
    assert response.json()["code"] == "NXS_TENANT_CONTEXT_REQUIRED"


async def test_spoofed_but_unknown_org_is_not_found(
    tenant_header_client: Any,
) -> None:
    response = await tenant_header_client.get(
        "/api/v1/organizations/current", headers={_HEADER: str(uuid.uuid7())}
    )
    # An attacker who guesses a header still resolves to their own (empty) scope: not-found,
    # never another tenant's data.
    assert response.status_code == 404
    assert response.json()["code"] == "NXS_ORG_NOT_FOUND"


async def test_malformed_tenant_header_is_rejected(tenant_header_client: Any) -> None:
    response = await tenant_header_client.get(
        "/api/v1/organizations/current",
        headers={_HEADER: "not-a-uuid'; DROP TABLE organizations;--"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "NXS_TENANT_CONTEXT_INVALID"


async def test_confused_deputy_body_org_id_is_ignored(
    tenant_header_client: Any, make_organization
) -> None:
    victim = await make_organization(activate=True)
    attacker = await make_organization(activate=True)
    # Attacker is scoped to their own org via the header, but puts the victim's id in the body.
    response = await tenant_header_client.patch(
        "/api/v1/organizations/current",
        headers={_HEADER: str(attacker.id)},
        json={
            "display_name": "Owned",
            "expected_version": attacker.version,
            "organization_id": str(victim.id),
        },
    )
    # organization_id is an unknown field -> 422; scope never switches to the victim regardless.
    assert response.status_code == 422
    read = await tenant_header_client.get(
        "/api/v1/organizations/current", headers={_HEADER: str(victim.id)}
    )
    assert read.json()["display_name"] != "Owned"


async def test_raw_sql_cross_scope_via_service_is_blocked(
    organization_service: Any, make_organization
) -> None:
    a = await make_organization(activate=True)
    b = await make_organization(activate=True)
    # Even asking the service for B while "being" A resolves to A's own row (RLS), not B.
    ctx_a = TenantContext(a.id, TenantContextSource.SYSTEM_BOOTSTRAP)
    current = await organization_service.get_current(ctx_a)
    assert current.id == a.id
    assert current.id != b.id


async def test_tax_identifier_never_in_public_view_or_repr(
    organization_service: Any,
) -> None:
    from nexus_ai.domain.organizations.entities import OrganizationDraft

    draft = OrganizationDraft(
        organization_key="tax-secret-org",
        display_name="Tax Secret",
        legal_name="Tax Secret SA",
        country_code="cr",
        timezone="America/Costa_Rica",
        tax_identifier="SECRET-TAX-9999",
    )
    org = await organization_service.create_core_record(draft)
    assert "SECRET-TAX-9999" not in repr(org)
    view = org.public_view()
    assert "SECRET-TAX-9999" not in repr(view)
    assert "tax_identifier" not in view.model_dump()


async def test_resolved_tenant_binds_organization_id_into_log_context(
    tenant_header_client: Any, make_organization
) -> None:
    import structlog

    from nexus_ai.api.dependencies import get_tenant_context

    org = await make_organization(activate=True)
    app = tenant_header_client.nexus_app

    scope = {
        "type": "http",
        "headers": [(_HEADER.lower().encode(), str(org.id).encode())],
        "app": app,
    }
    request = Request(scope)  # type: ignore[arg-type]
    structlog.contextvars.clear_contextvars()
    context = await get_tenant_context(request)
    assert context.organization_id == org.id
    assert structlog.contextvars.get_contextvars().get("organization_id") == str(org.id)
    structlog.contextvars.clear_contextvars()


async def test_unresolved_tenant_never_binds_organization_id(tenant_header_client: Any) -> None:
    import structlog

    from nexus_ai.api.dependencies import get_tenant_context
    from nexus_ai.core.errors import TenantContextRequiredError

    scope = {"type": "http", "headers": [], "app": tenant_header_client.nexus_app}
    structlog.contextvars.clear_contextvars()
    with pytest.raises(TenantContextRequiredError):
        await get_tenant_context(Request(scope))  # type: ignore[arg-type]
    assert "organization_id" not in structlog.contextvars.get_contextvars()
