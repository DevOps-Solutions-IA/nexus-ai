"""Customer/conversation API-level matrix (NXS-CUSTOMER-001).

Exercises every P06 endpoint over HTTP with real PostgreSQL — happy paths, error
paths and cursor pagination — through the bearer-resolved tenant context.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

PASSWORD = "correct-horse-battery-staple"


def _resources(client: Any) -> Any:
    return client.nexus_app.state.lifespan.resources  # type: ignore[attr-defined]


async def _login(client: Any, email: str, org_id: Any) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": PASSWORD, "organization_id": str(org_id)},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _bearer(session: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {session['access_token']}"}


class TestCustomerApi:
    async def test_full_customer_flow_over_http(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org)
        session = await _login(auth_client, email, org.id)

        # create (201), then resolve the same identity (200)
        identity_value = f"api-{uuid.uuid4().hex[:8]}@example.com"
        payload = {
            "display_name": "API Customer",
            "identity_type": "EMAIL",
            "identity_value": identity_value,
            "identity_source": "test",
        }
        created = await auth_client.post(
            "/api/v1/customers", headers=_bearer(session), json=payload
        )
        assert created.status_code == 201
        customer_id = created.json()["id"]
        resolved = await auth_client.post(
            "/api/v1/customers", headers=_bearer(session), json=payload
        )
        assert resolved.status_code == 200
        assert resolved.json()["id"] == customer_id

        # read + resolve-by-query
        read = await auth_client.get(f"/api/v1/customers/{customer_id}", headers=_bearer(session))
        assert read.status_code == 200
        lookup = await auth_client.get(
            "/api/v1/customers",
            headers=_bearer(session),
            params={"identity_type": "EMAIL", "identity": identity_value.upper()},
        )
        assert lookup.status_code == 200
        assert lookup.json()[0]["id"] == customer_id

        # link a phone identity through the API (idempotent re-link)
        link = {"identity_type": "PHONE", "identity_value": "+506 5555 4444"}
        linked = await auth_client.post(
            f"/api/v1/customers/{customer_id}/identities",
            headers=_bearer(session),
            json=link,
        )
        assert linked.status_code == 201
        relinked = await auth_client.post(
            f"/api/v1/customers/{customer_id}/identities",
            headers=_bearer(session),
            json=link,
        )
        assert relinked.status_code == 201
        identities = await auth_client.get(
            f"/api/v1/customers/{customer_id}/identities", headers=_bearer(session)
        )
        assert identities.status_code == 200
        assert len(identities.json()) == 2

    async def test_conversation_flow_over_http_with_errors(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org)
        session = await _login(auth_client, email, org.id)
        created = await auth_client.post(
            "/api/v1/customers",
            headers=_bearer(session),
            json={
                "display_name": "Conv Customer",
                "identity_type": "EMAIL",
                "identity_value": f"conv-{uuid.uuid4().hex[:8]}@example.com",
                "identity_source": "test",
            },
        )
        customer_id = created.json()["id"]

        opened = await auth_client.post(
            "/api/v1/conversations",
            headers=_bearer(session),
            json={"customer_id": customer_id, "channel": "web"},
        )
        assert opened.status_code == 201
        conversation_id = opened.json()["id"]

        read = await auth_client.get(
            f"/api/v1/conversations/{conversation_id}", headers=_bearer(session)
        )
        assert read.status_code == 200

        listed = await auth_client.get(
            f"/api/v1/customers/{customer_id}/conversations", headers=_bearer(session)
        )
        assert listed.status_code == 200
        assert len(listed.json()) == 1

        closed = await auth_client.post(
            f"/api/v1/conversations/{conversation_id}/close", headers=_bearer(session)
        )
        assert closed.status_code == 200
        assert closed.json()["status"] == "CLOSED"
        # idempotent close
        again = await auth_client.post(
            f"/api/v1/conversations/{conversation_id}/close", headers=_bearer(session)
        )
        assert again.status_code == 200

        # 404 for unknown resources
        missing_customer = await auth_client.get(
            f"/api/v1/customers/{uuid.uuid7()}", headers=_bearer(session)
        )
        assert missing_customer.status_code == 404
        missing_conversation = await auth_client.get(
            f"/api/v1/conversations/{uuid.uuid7()}", headers=_bearer(session)
        )
        assert missing_conversation.status_code == 404

        # timeline with cursor pagination
        timeline = await auth_client.get(
            f"/api/v1/customers/{customer_id}/timeline", headers=_bearer(session)
        )
        assert timeline.status_code == 200
        activities = timeline.json()
        assert len(activities) >= 3
        last = activities[-1]
        page_two = await auth_client.get(
            f"/api/v1/customers/{customer_id}/timeline",
            headers=_bearer(session),
            params={
                "after_occurred_at": last["occurred_at"],
                "after_id": last["id"],
                "limit": "5",
            },
        )
        assert page_two.status_code == 200
        assert page_two.json() == []

    async def test_reopen_and_external_key_resolution(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org)
        session = await _login(auth_client, email, org.id)
        opened = await auth_client.post(
            "/api/v1/conversations",
            headers=_bearer(session),
            json={
                "channel": "whatsapp",
                "provider_namespace": "wa",
                "external_thread_id": f"thr-{uuid.uuid4().hex[:8]}",
            },
        )
        assert opened.status_code == 201
        conversation_id = opened.json()["id"]
        # Same external thread resolves to the same conversation (200, not 201).
        replayed = await auth_client.post(
            "/api/v1/conversations",
            headers=_bearer(session),
            json={
                "channel": "whatsapp",
                "provider_namespace": "wa",
                "external_thread_id": opened.json()["external_thread_id"],
            },
        )
        assert replayed.status_code == 200
        assert replayed.json()["id"] == conversation_id
        # Close + reopen through the service seam (explicit CLOSED→OPEN).
        await auth_client.post(
            f"/api/v1/conversations/{conversation_id}/close", headers=_bearer(session)
        )
        resources = _resources(auth_client)
        reopened = await resources.conversations.reopen(org.id, uuid.UUID(conversation_id))
        assert reopened.status.value == "OPEN"

    async def test_invalid_identity_and_unsupported_type_over_http(
        self, auth_client: Any, make_auth_org: Any, make_auth_user: Any
    ) -> None:
        org = await make_auth_org()
        email, _, _ = await make_auth_user(organization=org)
        session = await _login(auth_client, email, org.id)
        invalid = await auth_client.post(
            "/api/v1/customers",
            headers=_bearer(session),
            json={
                "display_name": "Bad",
                "identity_type": "EMAIL",
                "identity_value": "not-an-email",
                "identity_source": "test",
            },
        )
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "NXS_CUSTOMER_IDENTITY_INVALID"
        unsupported = await auth_client.get(
            "/api/v1/customers",
            headers=_bearer(session),
            params={"identity_type": "SSN", "identity": "123-45-6789"},
        )
        assert unsupported.status_code == 422
        assert unsupported.json()["code"] == "NXS_CUSTOMER_IDENTITY_UNSUPPORTED"
        ambiguous_phone = await auth_client.post(
            "/api/v1/customers",
            headers=_bearer(session),
            json={
                "display_name": "Bad",
                "identity_type": "PHONE",
                "identity_value": "8888-7777",
                "identity_source": "test",
            },
        )
        assert ambiguous_phone.status_code == 422
        assert ambiguous_phone.json()["code"] == "NXS_CUSTOMER_IDENTITY_INVALID"
