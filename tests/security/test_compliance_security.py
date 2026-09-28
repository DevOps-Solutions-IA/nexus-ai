"""P03 identity/RBAC and actual PostgreSQL RLS remain the compliance boundary."""

import uuid

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import DBAPIError

from nexus_ai.compliance.contracts import ComplianceConflict, SubjectRequestInput
from nexus_ai.core.errors import NxsError, SessionRevokedError
from nexus_ai.domain.auth.rbac import RoleKey
from nexus_ai.domain.compliance.models import (
    ComplianceApproval,
    ComplianceControl,
    ComplianceExecution,
    ComplianceHold,
    CompliancePlan,
    CompliancePolicy,
    ComplianceRequest,
)
from tests.compliance_helpers import claimed, planned, request

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.mark.parametrize(
    "field",
    ["organization_id", "approver_id", "policy_revision", "handler", "url", "sql", "credential"],
)
async def test_api_rejects_forged_authority_fields(compliance_env, field):
    env = compliance_env
    response = await env.client.post(
        "/api/v1/compliance/requests",
        headers=env.headers,
        json={
            "subject_id": str(env.customer.id),
            "kind": "ACCESS",
            "idempotency_key": "safe",
            field: "forged",
        },
    )
    assert response.status_code == 422


async def test_ordinary_member_cannot_administer_compliance(
    compliance_env, make_auth_user, login_helper
):
    env = compliance_env
    email, password, _ = await make_auth_user(
        organization=env.organization, role=RoleKey.ORG_MEMBER
    )
    tokens = await login_helper(email, password)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    paths = [
        (
            "/policies",
            {"rules": [{"resource_class": "CUSTOMER_PROFILE", "days": 1, "action": "RETAIN"}]},
        ),
        ("/holds", {"subject_id": str(env.customer.id), "reason_code": "LEGAL"}),
        (
            "/requests",
            {"subject_id": str(env.customer.id), "kind": "ERASURE", "idempotency_key": "forged"},
        ),
        (f"/plans/{uuid.uuid4()}/approve", {"reason_code": "FORGED"}),
        (f"/plans/{uuid.uuid4()}/claim", {"owner_id": str(uuid.uuid4())}),
    ]
    for path, payload in paths:
        response = await env.client.post(f"/api/v1/compliance{path}", headers=headers, json=payload)
        assert response.status_code == 403, response.text


@pytest.mark.parametrize("mutation", ["membership", "session", "assignment"])
async def test_live_authorization_revocation_blocks_existing_token(compliance_env, mutation):
    env = compliance_env
    async with env.database.tenant_transaction(env.organization.id) as tenant:
        if mutation == "membership":
            await tenant.session.execute(
                text("UPDATE memberships SET status = 'SUSPENDED' WHERE user_id = :user"),
                {"user": env.principal.user_id},
            )
        elif mutation == "session":
            await tenant.session.execute(
                text("UPDATE refresh_sessions SET revoked_at = now() WHERE id = :id"),
                {"id": env.principal.session_id},
            )
        else:
            await tenant.session.execute(
                text("UPDATE role_assignments SET status = 'SUSPENDED' WHERE user_id = :user"),
                {"user": env.principal.user_id},
            )
    response = await env.client.get("/api/v1/compliance/requests", headers=env.headers)
    assert response.status_code in {401, 403}


async def test_direct_service_forged_principal_has_no_authority(compliance_env):
    env = compliance_env
    with pytest.raises(NxsError):
        await env.service.list_requests(env.principal.model_copy(update={"user_id": uuid.uuid4()}))
    with pytest.raises(SessionRevokedError):
        await env.service.list_requests(
            env.principal.model_copy(update={"session_id": uuid.uuid4()})
        )


async def test_missing_tenant_and_foreign_rows_fail_closed(compliance_env, make_auth_org):
    env = compliance_env
    row = await request(env)
    foreign = await make_auth_org()
    async with env.database.session() as session:
        assert (await session.execute(text("SELECT id FROM compliance_requests"))).all() == []
    async with env.database.tenant_transaction(foreign.id) as tenant:
        assert (
            await tenant.session.execute(text("SELECT id FROM compliance_requests"))
        ).all() == []
        result = await tenant.session.execute(
            text("UPDATE compliance_requests SET state = 'CANCELLED' WHERE id = :id"),
            {"id": uuid.UUID(row["id"])},
        )
        assert result.rowcount == 0
    with pytest.raises(DBAPIError):
        async with env.database.tenant_transaction(foreign.id) as tenant:
            await tenant.session.execute(
                text(
                    "INSERT INTO compliance_controls "
                    "(id, organization_id, policy_epoch, hold_epoch) "
                    "VALUES (:id, :org, 0, 0)"
                ),
                {"id": uuid.uuid4(), "org": env.organization.id},
            )
    with pytest.raises(DBAPIError):
        async with env.database.tenant_transaction(foreign.id) as tenant:
            await tenant.session.execute(
                text("DELETE FROM compliance_requests WHERE id = :id"), {"id": uuid.UUID(row["id"])}
            )


async def test_every_compliance_table_has_forced_rls_and_no_delete_ddl(compliance_env):
    env = compliance_env
    async with env.database.session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity, "
                    "has_table_privilege(current_user, oid, 'DELETE') FROM pg_class "
                    "WHERE relname LIKE 'compliance_%' AND relkind = 'r'"
                )
            )
        ).all()
        assert len(rows) == 7
        assert all(enabled and forced and not delete for _, enabled, forced, delete in rows)
        role = (
            await session.execute(
                text(
                    "SELECT current_user, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, "
                    "has_schema_privilege(current_user, 'public', 'CREATE') "
                    "FROM pg_roles WHERE rolname = current_user"
                )
            )
        ).one()
        assert role == ("nexus_runtime", False, False, False, False, False)


async def test_approval_and_plan_rows_cannot_be_rewritten(compliance_env):
    env = compliance_env
    _, plan = await planned(env)
    with pytest.raises(DBAPIError):
        async with env.database.tenant_transaction(env.organization.id) as tenant:
            await tenant.session.execute(
                text("UPDATE compliance_plans SET fingerprint = :hash WHERE id = :id"),
                {"hash": "0" * 64, "id": plan},
            )


async def test_api_auth_pagination_and_unknown_ids(compliance_env):
    env = compliance_env
    assert (await env.client.get("/api/v1/compliance/requests")).status_code == 401
    for limit in (0, 101, -1):
        assert (
            await env.client.get(f"/api/v1/compliance/requests?limit={limit}", headers=env.headers)
        ).status_code == 422
    assert (
        await env.client.get(f"/api/v1/compliance/requests/{uuid.uuid4()}", headers=env.headers)
    ).status_code == 404
    with pytest.raises(ComplianceConflict):
        await env.service.list_requests(env.principal, limit=100000)


async def test_foreign_subject_does_not_become_authority(compliance_env):
    env = compliance_env
    response = await env.client.post(
        "/api/v1/compliance/requests",
        headers=env.headers,
        json=SubjectRequestInput(
            subject_id=uuid.uuid4(), kind="ACCESS", idempotency_key="foreign"
        ).model_dump(mode="json"),
    )
    assert response.status_code == 404


async def test_all_p21_tables_reject_foreign_reads_inserts_updates(compliance_env, make_auth_org):
    env = compliance_env
    await claimed(env)
    from nexus_ai.compliance.contracts import HoldInput

    await env.service.create_hold(
        env.principal, HoldInput(subject_id=env.customer.id, reason_code="LEGAL")
    )
    foreign = await make_auth_org()
    for model in (
        ComplianceControl,
        CompliancePolicy,
        ComplianceHold,
        ComplianceRequest,
        CompliancePlan,
        ComplianceApproval,
        ComplianceExecution,
    ):
        async with env.database.tenant_transaction(env.organization.id) as tenant:
            row = await tenant.session.scalar(select(model))
            values = {column.name: getattr(row, column.name) for column in model.__table__.columns}
            values["id"] = uuid.uuid4()
        async with env.database.tenant_transaction(foreign.id) as tenant:
            assert (await tenant.session.scalars(select(model))).all() == []
        with pytest.raises(DBAPIError) as denied:
            async with env.database.tenant_transaction(foreign.id) as tenant:
                await tenant.session.execute(insert(model).values(**values))
        assert "row-level security" in str(denied.value)
        if model not in (CompliancePlan, ComplianceApproval):
            async with env.database.tenant_transaction(foreign.id) as tenant:
                changed = await tenant.session.execute(update(model).values(id=uuid.uuid4()))
                assert changed.rowcount == 0


async def test_compliance_recheck_uses_single_connection_pool(compliance_env, pool1_database):
    from nexus_ai.compliance.service import ComplianceService

    env = compliance_env
    service = ComplianceService(
        pool1_database, env.resources.event_platform.publisher, service_name="compliance-test"
    )
    assert await service.list_requests(env.principal) == []
