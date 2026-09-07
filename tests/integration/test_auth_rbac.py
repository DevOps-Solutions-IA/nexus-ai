"""Organization-scoped RBAC matrix (NXS-AUTH-006).

Deny-by-default permission checks, inactive role assignments, forged role
identifiers, privilege escalation attempts and cross-tenant role reuse — resolved
against the seeded catalogs and tenant-scoped assignments in real PostgreSQL.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus_ai.core.errors import PermissionDeniedError
from nexus_ai.domain.auth.entities import Principal
from nexus_ai.domain.auth.rbac import AuthorizationService, PermissionKey, RoleKey

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

PASSWORD = "correct-horse-battery-staple"


async def _principal_for(resources: Any, login_helper: Any, email: str) -> Principal:
    session = await login_helper(email, PASSWORD)
    claims = resources.token_service.verify_access_token(session["access_token"])
    return Principal(
        user_id=claims.subject,
        session_id=claims.session_id,
        organization_id=claims.organization_id,
        token_id=claims.jti,
        issued_at=claims.issued_at,
        expires_at=claims.expires_at,
    )


class TestAuthorization:
    async def test_owner_holds_all_org_permissions(
        self,
        auth_client: Any,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
        tenant_database: Any,
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org, role=RoleKey.ORG_OWNER)
        resources = _resources(auth_client)
        principal = await _principal_for(resources, login_helper, email)
        authorizer = AuthorizationService(tenant_database)
        async with tenant_database.tenant_transaction(org.id) as tenant:
            for permission in PermissionKey:
                await authorizer.require(principal, permission, tenant=tenant)

    async def test_member_denied_by_default(
        self,
        auth_client: Any,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
        tenant_database: Any,
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org, role=RoleKey.ORG_MEMBER)
        resources = _resources(auth_client)
        principal = await _principal_for(resources, login_helper, email)
        authorizer = AuthorizationService(tenant_database)
        async with tenant_database.tenant_transaction(org.id) as tenant:
            # Read is granted; writes are deny-by-default.
            await authorizer.require(principal, PermissionKey.ORGANIZATION_READ, tenant=tenant)
            for denied in (
                PermissionKey.ORGANIZATION_WRITE,
                PermissionKey.MEMBERSHIP_MANAGE,
                PermissionKey.ROLE_ASSIGN,
            ):
                with pytest.raises(PermissionDeniedError):
                    await authorizer.require(principal, denied, tenant=tenant)

    async def test_forged_role_identifier_grants_nothing(
        self,
        auth_client: Any,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
        tenant_database: Any,
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org, role=RoleKey.ORG_MEMBER)
        resources = _resources(auth_client)
        principal = await _principal_for(resources, login_helper, email)
        authorizer = AuthorizationService(tenant_database)
        async with tenant_database.tenant_transaction(org.id) as tenant:
            with pytest.raises(PermissionDeniedError):
                await authorizer.require(principal, PermissionKey.ROLE_ASSIGN, tenant=tenant)

    async def test_suspended_assignment_grants_nothing(
        self,
        auth_client: Any,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
        tenant_database: Any,
    ) -> None:
        org = await make_auth_org()
        admin_email, _, _ = await make_auth_user(organization=org, role=RoleKey.ORG_OWNER)
        member_email, _, member = await make_auth_user(organization=org, role=RoleKey.ORG_ADMIN)
        resources = _resources(auth_client)
        admin = await _principal_for(resources, login_helper, admin_email)
        # Suspend the admin assignment of the member.
        async with tenant_database.tenant_transaction(org.id) as tenant:
            from sqlalchemy import text

            assignment_id = (
                (
                    await tenant.session.execute(
                        text(
                            "SELECT ra.id FROM role_assignments ra "
                            "JOIN roles r ON r.id = ra.role_id "
                            "WHERE ra.user_id = :u AND r.role_key = 'org_admin'"
                        ),
                        {"u": member.id},
                    )
                )
                .scalars()
                .one()
            )
            await resources.memberships.suspend_assignment(
                actor=admin, organization_id=org.id, assignment_id=assignment_id
            )
        member_principal = await _principal_for(resources, login_helper, member_email)
        authorizer = AuthorizationService(tenant_database)
        async with tenant_database.tenant_transaction(org.id) as tenant:
            with pytest.raises(PermissionDeniedError):
                await authorizer.require(
                    member_principal, PermissionKey.MEMBERSHIP_MANAGE, tenant=tenant
                )

    async def test_role_from_another_organization_grants_nothing_here(
        self,
        auth_client: Any,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
        tenant_database: Any,
    ) -> None:
        org_a = await make_auth_org()
        org_b = await make_auth_org()
        # Owner in org A, plain member in org B.
        email, _, user = await make_auth_user(organization=org_a, role=RoleKey.ORG_OWNER)
        resources = _resources(auth_client)
        await resources.memberships.create(
            actor=None, organization_id=org_b.id, user_id=user.id, role=RoleKey.ORG_MEMBER
        )
        # Login into org B: the org-A ownership must NOT carry over.
        session = await login_helper(email, PASSWORD, organization_id=org_b.id)
        claims = resources.token_service.verify_access_token(session["access_token"])
        principal = Principal(
            user_id=claims.subject,
            session_id=claims.session_id,
            organization_id=claims.organization_id,
            token_id=claims.jti,
            issued_at=claims.issued_at,
            expires_at=claims.expires_at,
        )
        authorizer = AuthorizationService(tenant_database)
        async with tenant_database.tenant_transaction(org_b.id) as tenant:
            with pytest.raises(PermissionDeniedError):
                await authorizer.require(principal, PermissionKey.MEMBERSHIP_MANAGE, tenant=tenant)

    async def test_privilege_escalation_via_membership_creation_denied(
        self,
        auth_client: Any,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
    ) -> None:
        org = await make_auth_org()
        admin_email, _, _ = await make_auth_user(organization=org, role=RoleKey.ORG_OWNER)
        member_email, _, member = await make_auth_user(organization=org, role=RoleKey.ORG_MEMBER)
        victim_email, _, _ = await make_auth_user(organization=org, role=RoleKey.ORG_MEMBER)
        resources = _resources(auth_client)
        member_principal = await _principal_for(resources, login_helper, member_email)
        # A member cannot grant themselves a role in this org.
        with pytest.raises(PermissionDeniedError):
            await resources.memberships.assign_role(
                actor=member_principal,
                organization_id=org.id,
                user_id=member.id,
                role=RoleKey.ORG_OWNER,
            )
        # An owner CAN manage members.
        admin = await _principal_for(resources, login_helper, admin_email)
        from nexus_ai.domain.auth.repository import UserRepository

        async with resources.database.transaction() as session:
            victim = await UserRepository(session).by_email(victim_email)
        assert victim is not None
        await resources.memberships.suspend(actor=admin, organization_id=org.id, user_id=victim.id)
        # The suspended victim can no longer log in.
        blocked = await auth_client.post(
            "/api/v1/auth/login", json={"email": victim_email, "password": PASSWORD}
        )
        assert blocked.status_code == 403

    async def test_unknown_role_key_denied(
        self,
        auth_client: Any,
        make_auth_org: Any,
        make_auth_user: Any,
        login_helper: Any,
    ) -> None:
        org = await make_auth_org()
        admin_email, _, _ = await make_auth_user(organization=org, role=RoleKey.ORG_OWNER)
        _member_email, _, member = await make_auth_user(organization=org, role=RoleKey.ORG_MEMBER)
        resources = _resources(auth_client)
        admin = await _principal_for(resources, login_helper, admin_email)
        from typing import cast

        forged_role = cast(RoleKey, "super_admin_global")
        with pytest.raises(PermissionDeniedError, match="Unknown role"):
            await resources.memberships.assign_role(
                actor=admin,
                organization_id=org.id,
                user_id=member.id,
                role=forged_role,
            )


def _resources(client: Any) -> Any:
    return client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]
