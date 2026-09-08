"""Integration Hub stable contracts — error taxonomy, event payloads, result shape,
API surface (NXS-INT-001)."""

from __future__ import annotations

import pytest

from nexus_ai.core.errors import NxsError
from nexus_ai.events.registry import EVENT_REGISTRY
from nexus_ai.integrations.entities import IntegrationResult, ResultClass
from nexus_ai.integrations.errors import INTEGRATION_ERRORS

pytestmark = pytest.mark.anyio


def test_error_taxonomy_is_stable_and_unique() -> None:
    codes = [e.code for e in INTEGRATION_ERRORS]
    assert len(codes) == len(set(codes))
    for error in INTEGRATION_ERRORS:
        assert error.code.startswith("NXS_INT_")
        assert issubclass(error, NxsError)
        assert 400 <= error.status <= 599
        assert error.title and error.title != NxsError.title
    # retry classification is deliberate on transient categories
    retryable = {e.code for e in INTEGRATION_ERRORS if e.retryable}
    assert "NXS_INT_TIMEOUT" in retryable
    assert "NXS_INT_UPSTREAM_SERVER_ERROR" in retryable
    assert "NXS_INT_CIRCUIT_OPEN" in retryable
    assert "NXS_INT_UPSTREAM_CLIENT_ERROR" not in retryable
    assert "NXS_INT_DESTINATION_BLOCKED" not in retryable


def test_event_payloads_are_registered_and_strict() -> None:
    for event_type in (
        "integrations.created",
        "integrations.updated",
        "integrations.disabled",
        "integrations.operation.created",
        "integrations.execution.failed",
        "integrations.webhook.received",
    ):
        assert EVENT_REGISTRY.is_known_type(event_type)
        model = EVENT_REGISTRY.model_for(event_type, 1)
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            model.model_validate({"unexpected_field": 1})


def test_integration_result_round_trips() -> None:
    import uuid

    result = IntegrationResult(
        integration_id=uuid.uuid4(),
        operation_key="crm.get",
        result_class=ResultClass.SUCCESS,
        ok=True,
        status_code=200,
        output={"id": "c1"},
        config_revision=3,
    )
    restored = IntegrationResult.model_validate(result.model_dump(mode="json"))
    assert restored == result


async def test_api_has_no_arbitrary_proxy_and_is_operation_based(app_client) -> None:  # type: ignore[no-untyped-def]
    schema = (await app_client.get("/openapi.json")).json()
    paths = set(schema["paths"])
    assert not any("/http/request" in p or "/proxy" in p for p in paths)
    for expected in (
        "/api/v1/integrations",
        "/api/v1/integrations/{integration_id}",
        "/api/v1/integrations/{integration_id}/operations",
        "/api/v1/integrations/{integration_id}/execute",
        "/api/v1/integrations/{integration_id}/test",
        "/api/v1/integrations/openapi/import",
        "/api/v1/integrations/{integration_id}/webhooks",
        "/api/v1/integrations/webhooks/{token}",
    ):
        assert expected in paths, expected


@pytest.mark.integration
async def test_execute_requires_permission(auth_client, make_auth_user, make_auth_org) -> None:  # type: ignore[no-untyped-def]
    from nexus_ai.domain.auth.rbac import RoleKey

    org = await make_auth_org()
    email, password, _ = await make_auth_user(organization=org, role=RoleKey.ORG_MEMBER)
    login = await auth_client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    token = login.json()["access_token"]
    import uuid

    # A member has integration:execute but not integration:create.
    created = await auth_client.post(
        "/api/v1/integrations",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "slug": "x",
            "name": "x",
            "integration_type": "REST",
            "base_url": "https://api.example.com",
        },
    )
    assert created.status_code == 403
    ghost = uuid.uuid4()
    missing = await auth_client.post(
        f"/api/v1/integrations/{ghost}/execute",
        headers={"Authorization": f"Bearer {token}"},
        json={"integration_id": str(ghost), "operation_key": "op.x", "input": {}},
    )
    # authorized (member has execute) but the integration does not exist -> 404
    assert missing.status_code == 404, missing.text
