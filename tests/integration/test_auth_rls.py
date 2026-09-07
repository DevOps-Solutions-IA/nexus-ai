"""Raw-SQL RLS adversarial matrix for the P03 tables (NXS-AUTH-002/004/006).

The tenant boundary for memberships, role_assignments and refresh_sessions is a
PostgreSQL guarantee, not an application convention: raw SQL through the non-bypass
runtime role sees exactly the transaction-local Organization's rows (plus the
caller's own membership rows when the principal GUC is bound without org scope), and
cross-tenant writes fail at the database.
"""

from __future__ import annotations

from typing import Any

import pytest
from asyncpg.exceptions import InsufficientPrivilegeError
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.anyio


class TestMembershipsRls:
    async def test_raw_sql_cannot_read_foreign_org_memberships(
        self,
        tenant_database: Any,
        make_organization: Any,
        make_auth_user: Any,
        auth_client: Any,
    ) -> None:
        org_a = await make_organization()
        org_b = await make_organization()
        _email_a, _pw, user_a = await make_auth_user(organization=org_a)
        _email_b, _pw, user_b = await make_auth_user(organization=org_b)

        async with tenant_database.tenant_transaction(org_a.id) as tenant:
            rows = (
                await tenant.session.execute(  # no WHERE clause at all — RLS filters
                    _raw_select("SELECT user_id FROM memberships")
                )
            ).scalars()
            visible = {str(r) for r in rows}
        assert str(user_a.id) in visible
        assert str(user_b.id) not in visible  # foreign org's member is invisible

    async def test_raw_sql_cannot_insert_foreign_org_membership(
        self,
        tenant_database: Any,
        make_organization: Any,
        make_auth_user: Any,
        auth_client: Any,
    ) -> None:
        org_a = await make_organization()
        org_b = await make_organization()
        _email, _pw, user = await make_auth_user(organization=org_a)

        async with tenant_database.tenant_transaction(org_a.id) as tenant:
            with pytest.raises(DBAPIError):
                await tenant.session.execute(
                    _raw_sql(
                        "INSERT INTO memberships (id, organization_id, user_id, status) "
                        "VALUES (:id, :org, :user, 'ACTIVE')",
                        {"id": _uuid(), "org": org_b.id, "user": user.id},
                    )
                )

    async def test_raw_sql_cannot_update_foreign_org_membership(
        self,
        tenant_database: Any,
        make_organization: Any,
        make_auth_user: Any,
        auth_client: Any,
    ) -> None:
        org_a = await make_organization()
        org_b = await make_organization()
        _email, _pw, user_b = await make_auth_user(organization=org_b)
        # Scoped to org A, an UPDATE aimed at org B's rows must match zero rows.
        async with tenant_database.tenant_transaction(org_a.id) as tenant:
            result = await tenant.session.execute(
                _raw_sql(
                    "UPDATE memberships SET status='REVOKED' WHERE user_id = :user",
                    {"user": user_b.id},
                )
            )
            assert result.rowcount == 0  # type: ignore[attr-defined]
        # And the row is untouched.
        async with tenant_database.tenant_transaction(org_b.id) as tenant:
            status = (
                await tenant.session.execute(_raw_select("SELECT status FROM memberships"))
            ).scalars()
            assert list(status) == ["ACTIVE"]

    async def test_no_org_scope_means_zero_visibility(
        self,
        tenant_database: Any,
        make_organization: Any,
        make_auth_user: Any,
        auth_client: Any,
    ) -> None:
        org_a = await make_organization()
        await make_auth_user(organization=org_a)
        async with tenant_database.session() as session, session.begin():
            rows = (await session.execute(_raw_select("SELECT id FROM memberships"))).scalars()
            assert list(rows) == []  # no context, no rows: never a global fallback

    async def test_principal_scope_shows_only_own_rows(
        self,
        tenant_database: Any,
        make_organization: Any,
        make_auth_user: Any,
        auth_client: Any,
    ) -> None:
        org_a = await make_organization()
        org_b = await make_organization()
        _email_a, _pw, user_a = await make_auth_user(organization=org_a)
        await make_auth_user(organization=org_b)

        async with tenant_database.principal_session(user_a.id) as session:
            rows = (
                await session.execute(_raw_select("SELECT organization_id FROM memberships"))
            ).scalars()
            visible = {str(r) for r in rows}
        assert visible == {str(org_a.id)}  # own rows only, across ALL orgs

    async def test_principal_scope_does_not_leak_when_org_scope_bound(
        self,
        tenant_database: Any,
        make_organization: Any,
        make_auth_user: Any,
        auth_client: Any,
    ) -> None:
        org_a = await make_organization()
        org_b = await make_organization()
        _email, _pw, user = await make_auth_user(organization=org_a)
        resources = _auth_resources(auth_client)
        await resources.memberships.create(
            actor=None,
            organization_id=org_b.id,
            user_id=user.id,
            role=_org_member(),
        )
        # Bound to org A, the principal's foreign rows must NOT appear.
        async with tenant_database.tenant_transaction(org_a.id) as tenant:
            rows = (
                await tenant.session.execute(_raw_select("SELECT organization_id FROM memberships"))
            ).scalars()
            visible = {str(r) for r in rows}
        assert visible == {str(org_a.id)}


class TestRefreshSessionsRls:
    async def test_session_rows_invisible_cross_tenant(
        self,
        tenant_database: Any,
        make_organization: Any,
        make_auth_user: Any,
        login_helper: Any,
        auth_client: Any,
    ) -> None:
        org_a = await make_organization()
        org_b = await make_organization()
        email_a, _pw, _user = await make_auth_user(organization=org_a)
        await login_helper(email_a, PASSWORD)

        async with tenant_database.tenant_transaction(org_b.id) as tenant:
            rows = (
                await tenant.session.execute(_raw_select("SELECT id FROM refresh_sessions"))
            ).scalars()
            assert list(rows) == []  # org B sees no session rows from org A


class TestRoleAssignmentsRls:
    async def test_assignments_invisible_cross_tenant(
        self,
        tenant_database: Any,
        make_organization: Any,
        make_auth_user: Any,
        auth_client: Any,
    ) -> None:
        org_a = await make_organization()
        org_b = await make_organization()
        await make_auth_user(organization=org_a)

        async with tenant_database.tenant_transaction(org_b.id) as tenant:
            rows = (
                await tenant.session.execute(_raw_select("SELECT id FROM role_assignments"))
            ).scalars()
            assert list(rows) == []

    async def test_cross_tenant_assignment_insert_fails_at_database(
        self,
        tenant_database: Any,
        make_organization: Any,
        make_auth_user: Any,
        auth_client: Any,
    ) -> None:
        org_a = await make_organization()
        org_b = await make_organization()
        _email, _pw, user = await make_auth_user(organization=org_a)
        resources = _auth_resources(auth_client)
        role_id = await _role_id(resources, "org_admin")

        async with tenant_database.tenant_transaction(org_a.id) as tenant:
            with pytest.raises(DBAPIError):
                await tenant.session.execute(
                    _raw_sql(
                        "INSERT INTO role_assignments "
                        "(id, organization_id, user_id, role_id, status) "
                        "VALUES (:id, :org, :user, :role, 'ACTIVE')",
                        {"id": _uuid(), "org": org_b.id, "user": user.id, "role": role_id},
                    )
                )


class TestRuntimePrivileges:
    """Raw asyncpg raises InsufficientPrivilegeError directly (no SQLAlchemy wrapper)."""

    async def test_runtime_role_has_no_delete_on_auth_tables(
        self,
        raw_runtime_connection: Any,
        make_organization: Any,
        make_auth_user: Any,
        auth_client: Any,
    ) -> None:
        org = await make_organization()
        await make_auth_user(organization=org)
        with pytest.raises(InsufficientPrivilegeError):
            await raw_runtime_connection.execute("DELETE FROM memberships")

    async def test_runtime_role_has_no_ddl(
        self,
        raw_runtime_connection: Any,
    ) -> None:
        with pytest.raises(InsufficientPrivilegeError):
            await raw_runtime_connection.execute("ALTER TABLE users ADD COLUMN evil text")


PASSWORD = "correct-horse-battery-staple"


def _auth_resources(client: Any) -> Any:
    return client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]


def _org_member() -> Any:
    from nexus_ai.domain.auth.rbac import RoleKey

    return RoleKey.ORG_MEMBER


async def _role_id(resources: Any, role_key: str) -> Any:
    from sqlalchemy import text

    async with resources.database.session() as session:
        row = (
            await session.execute(text("SELECT id FROM roles WHERE role_key = :k"), {"k": role_key})
        ).scalars()
        return row.one()


def _raw_sql(sql: str, params: dict[str, Any]) -> Any:
    from sqlalchemy import text

    return text(sql).bindparams(**params)


def _raw_select(sql: str) -> Any:
    from sqlalchemy import text

    return text(sql)


def _uuid() -> Any:
    import uuid

    return uuid.uuid7()
