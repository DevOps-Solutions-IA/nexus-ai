from uuid import uuid4

import pytest
from sqlalchemy import select

from nexus_ai.audit.context import principal_actor
from nexus_ai.audit.contracts import AuditIntent
from nexus_ai.core.context import request_context
from nexus_ai.core.errors import PermissionDeniedError, SessionRevokedError
from nexus_ai.domain.auth.repository import RefreshSessionRepository
from nexus_ai.domain.customers.entities import CreateCustomerRequest
from nexus_ai.domain.customers.models import CustomerRecord
from nexus_ai.domain.events.models import EventOutboxRecord

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _intents(database, organization_id):
    async with database.tenant_transaction(organization_id) as tenant:
        rows = await tenant.session.scalars(
            select(EventOutboxRecord).where(EventOutboxRecord.event_type == AuditIntent.EVENT_TYPE)
        )
        return [AuditIntent.model_validate(row.envelope["payload"]) for row in rows]


async def test_customer_http_actor_and_no_private_identity_capture(
    auth_client, make_auth_org, make_auth_user, login_helper
):
    organization = await make_auth_org()
    email, password, user = await make_auth_user(organization=organization)
    tokens = await login_helper(email, password)
    response = await auth_client.post(
        "/api/v1/customers",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json={
            "display_name": "Private name",
            "identity_type": "EMAIL",
            "identity_value": "private-identity@example.com",
            "identity_source": "api",
        },
    )
    assert response.status_code == 201, response.text
    database = auth_client.nexus_app.state.lifespan.resources.database
    intents = await _intents(database, organization.id)
    customers = [intent for intent in intents if intent.producer == "customer"]
    assert customers
    assert all(
        intent.actor.kind == "HUMAN" and intent.actor.user_id == user.id for intent in customers
    )
    assert all(intent.actor.session_id is not None for intent in customers)
    assert all("private-identity" not in intent.model_dump_json() for intent in customers)
    assert all("Private name" not in intent.model_dump_json() for intent in customers)


async def test_customer_missing_http_actor_rolls_back(auth_client, make_auth_org):
    organization = await make_auth_org()
    resources = auth_client.nexus_app.state.lifespan.resources
    with request_context(), pytest.raises(PermissionDeniedError):
        await resources.customers.resolve_or_create(
            organization.id,
            CreateCustomerRequest(
                display_name="Rollback",
                identity_type="EMAIL",
                identity_value="rollback@example.com",
                identity_source="api",
            ),
        )
    async with resources.database.tenant_transaction(organization.id) as tenant:
        assert list(await tenant.session.scalars(select(CustomerRecord))) == []
    assert not [
        intent
        for intent in await _intents(resources.database, organization.id)
        if intent.producer == "customer"
    ]


async def test_audit_insert_failure_rolls_back_customer(auth_client, make_auth_org, monkeypatch):
    organization = await make_auth_org()
    resources = auth_client.nexus_app.state.lifespan.resources

    async def fail(*args, **kwargs):
        raise RuntimeError("injected audit persistence failure")

    monkeypatch.setattr("nexus_ai.domain.customers.service.emit_audit", fail)
    with pytest.raises(RuntimeError, match="injected audit"):
        await resources.customers.resolve_or_create(
            organization.id,
            CreateCustomerRequest(
                display_name="Rollback",
                identity_type="EMAIL",
                identity_value="rollback@example.com",
                identity_source="api",
            ),
        )
    async with resources.database.tenant_transaction(organization.id) as tenant:
        assert list(await tenant.session.scalars(select(CustomerRecord))) == []


async def test_principal_requires_real_owned_live_session(
    make_organization, make_tool_principal, tenant_database
):
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        actor = await principal_actor(tenant_database, tenant, principal)
        assert actor.user_id == principal.user_id
        with pytest.raises(SessionRevokedError):
            await principal_actor(
                tenant_database, tenant, principal.model_copy(update={"session_id": uuid4()})
            )
        await RefreshSessionRepository(tenant).revoke(principal.session_id)
    async with tenant_database.tenant_transaction(organization.id) as tenant:
        with pytest.raises(SessionRevokedError):
            await principal_actor(tenant_database, tenant, principal)


async def test_self_suspension_keeps_pre_mutation_human_provenance(
    auth_client, make_organization, make_tool_principal
):
    organization = await make_organization()
    principal = await make_tool_principal(organization)
    resources = auth_client.nexus_app.state.lifespan.resources
    await resources.memberships.suspend(
        actor=principal, organization_id=organization.id, user_id=principal.user_id
    )
    facts = [
        intent
        for intent in await _intents(resources.database, organization.id)
        if intent.action == "auth.membership.suspended"
    ]
    assert len(facts) == 1
    assert facts[0].actor.kind == "HUMAN"
    assert facts[0].actor.user_id == principal.user_id
    assert facts[0].actor.session_id == principal.session_id


@pytest.mark.parametrize("fail_dispatch", [False, True])
async def test_tool_durable_attempt_and_receipt_failure_boundaries(
    tool_engine,
    make_organization,
    make_tool_principal,
    mock_http_server,
    monkeypatch,
    fail_dispatch,
):
    from sqlalchemy import text

    from nexus_ai.audit.producer import emit_audit
    from nexus_ai.tools.entities import ToolInvocation
    from tests.integration.test_tool_engine import _ready_integration, _ready_tool

    organization = await make_organization()
    principal = await make_tool_principal(organization)
    integration = await _ready_integration(tool_engine, organization.id, mock_http_server.base_url)
    await _ready_tool(tool_engine, organization.id, integration.id)
    effects = []

    def effect(*args):
        effects.append(True)
        return 200, {"id": "c1"}

    async def fail(*args, **kwargs):
        if not fail_dispatch and kwargs["action"] == "tool.execution.dispatch_authorized":
            return await emit_audit(*args, **kwargs)
        raise RuntimeError("injected audit insertion failure")

    mock_http_server.set_handler(effect)
    monkeypatch.setattr("nexus_ai.tools.service.emit_audit", fail)
    with pytest.raises(RuntimeError, match="injected audit"):
        await tool_engine.engine.invoke(
            principal,
            ToolInvocation(tool_key="crm.get_contact", arguments={"path_params": {"id": "c1"}}),
        )
    assert len(effects) == (0 if fail_dispatch else 1)
    async with tool_engine.database.tenant_transaction(organization.id) as tenant:
        assert await tenant.session.scalar(text("SELECT count(*) FROM tool_execution_records")) == 0
    facts = [
        intent
        for intent in await _intents(tool_engine.database, organization.id)
        if intent.producer == "tool"
    ]
    assert len(facts) == (0 if fail_dispatch else 1)
    if facts:
        assert facts[0].action == "tool.execution.dispatch_authorized"
        assert facts[0].actor.user_id == principal.user_id
