"""Versioned API end-to-end, including real outbox and trusted principal derivation."""

import uuid

import pytest

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_authenticated_api_complete_profile_workflow(compliance_env):
    env = compliance_env

    async def post(path, payload=None):
        response = await env.client.post(
            f"/api/v1/compliance{path}", headers=env.headers, json=payload
        )
        assert response.status_code in {200, 204}, response.text
        return response.json() if response.status_code != 204 else None

    policy = await post(
        "/policies",
        {"rules": [{"resource_class": "CUSTOMER_PROFILE", "days": 30, "action": "ANONYMIZE"}]},
    )
    await post(f"/policies/{policy['id']}/activate")
    held = await post("/holds", {"subject_id": str(env.customer.id), "reason_code": "LEGAL"})
    await post(f"/holds/{held['id']}/release")
    row = await post(
        "/requests",
        {"subject_id": str(env.customer.id), "kind": "ACCESS", "idempotency_key": "access"},
    )
    await post(
        f"/requests/{row['id']}/verify", {"method_code": "OPERATOR", "evidence_reference": "opaque"}
    )
    plan = await post(f"/requests/{row['id']}/plan")
    await post(f"/plans/{plan['id']}/approve", {"reason_code": "APPROVED"})
    owner = str(uuid.uuid4())
    claim = await post(f"/plans/{plan['id']}/claim", {"owner_id": owner})
    result = await post(
        f"/plans/{plan['id']}/execute", {"owner_id": owner, "generation": claim["generation"]}
    )
    assert result["result"]["display_name"] == "Compliance subject"
    response = await env.client.get(f"/api/v1/compliance/requests/{row['id']}", headers=env.headers)
    assert response.json()["state"] == "PARTIAL"
    response = await env.client.get("/api/v1/compliance/requests?limit=1", headers=env.headers)
    assert len(response.json()) == 1
    retained = await post(
        "/retention/evaluate", {"subject_id": str(env.customer.id), "idempotency_key": "retain"}
    )
    assert retained["action"] == "RETAIN"
    await post(f"/policies/{policy['id']}/retire")
    denied = await post(
        "/requests",
        {"subject_id": str(env.customer.id), "kind": "ERASURE", "idempotency_key": "denied"},
    )
    await post(f"/requests/{denied['id']}/decisions/DENIED")
