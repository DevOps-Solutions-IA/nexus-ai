import datetime as dt
import uuid

import pytest

from nexus_ai.audit.contracts import AuditActor, AuditIntent
from nexus_ai.audit.repository import AuditRepository

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_snapshot_pagination_excludes_concurrent_append(tenant_database, make_organization):
    organization = await make_organization()

    def intent():
        return AuditIntent(
            organization_id=organization.id,
            source_id=uuid.uuid4(),
            producer="customer",
            action="customer.created",
            target_type="customer",
            target_id=uuid.uuid4(),
            actor=AuditActor(kind="HUMAN", user_id=uuid.uuid4()),
            occurred_at=dt.datetime.now(dt.UTC),
        )

    async with tenant_database.tenant_transaction(organization.id) as tenant:
        repository = AuditRepository(tenant)
        for _ in range(3):
            await repository.append(intent())
        page = await repository.page(limit=2)
        assert [row["sequence"] for row in page["records"]] == [1, 2]
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        await AuditRepository(tenant).append(intent())
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        page = await AuditRepository(tenant).page(limit=2, after=2, high_water=page["high_water"])
        assert [row["sequence"] for row in page["records"]] == [3]
        assert page["next_after"] is None
        partial = await AuditRepository(tenant).verify(limit=2)
        assert partial.valid and not partial.complete and partial.next_start == 3


async def test_query_requires_live_owner_and_bounds(compliance_env):
    env = compliance_env
    response = await env.client.get("/api/v1/audit/records", headers=env.headers)
    assert response.status_code == 200, response.text
    for query in ("limit=101", "limit=0", "after=-1", "action=SELECT%20secret", "unknown=1"):
        response = await env.client.get(f"/api/v1/audit/records?{query}", headers=env.headers)
        assert response.status_code == 422, response.text
    response = await env.client.get("/api/v1/audit/verify?limit=1001", headers=env.headers)
    assert response.status_code == 422
    response = await env.client.get(f"/api/v1/audit/records/{uuid.uuid4()}", headers=env.headers)
    assert response.status_code == 404


async def test_member_denied_and_owner_revocation(
    compliance_env,
    make_auth_user,
    login_helper,
):
    from sqlalchemy import text

    from nexus_ai.domain.auth.rbac import RoleKey

    env = compliance_env
    email, password, _ = await make_auth_user(
        organization=env.organization, role=RoleKey.ORG_MEMBER
    )
    tokens = await login_helper(email, password)
    member_headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    for endpoint in ("records", "verify"):
        response = await env.client.get(f"/api/v1/audit/{endpoint}", headers=member_headers)
        assert response.status_code == 403, response.text
    async with env.database.tenant_transaction(env.organization.id) as tenant:
        await tenant.session.execute(
            text("UPDATE memberships SET status='SUSPENDED' WHERE user_id=:user"),
            {"user": env.principal.user_id},
        )
    response = await env.client.get("/api/v1/audit/records", headers=env.headers)
    assert response.status_code in (401, 403), response.text
