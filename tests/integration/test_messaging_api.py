"""Messaging Channels HTTP API surface (NXS-P09)."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any

import pytest

from nexus_ai.domain.auth.rbac import RoleKey

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


class _FakeTransport:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def request(self, *, method, url, headers, body, timeout_seconds=None):  # type: ignore[no-untyped-def]
        from nexus_ai.messaging.providers.base import TransportResponse

        self.requests.append({"url": url, "headers": dict(headers), "body": body})
        return TransportResponse(
            status_code=200,
            headers={},
            body=b'{"message_id": "prov-1", "messages": [{"id": "prov-1"}]}',
        )


def _rewire(auth_client: Any) -> _FakeTransport:
    from cryptography.fernet import Fernet

    from nexus_ai.domain.customers.service import ConversationService, CustomerService
    from nexus_ai.domain.messaging.repository import MessagingSecretStore
    from nexus_ai.integrations.credentials import LocalEncryptedVault, build_fernet
    from nexus_ai.messaging.service import MessagingService
    from nexus_ai.messaging.webhooks import InboundMessagingService

    resources = auth_client.nexus_app.state.lifespan.resources
    settings = resources.settings
    transport = _FakeTransport()
    vault = LocalEncryptedVault(
        MessagingSecretStore(resources.database), build_fernet([Fernet.generate_key().decode()])
    )
    customers = CustomerService(settings, resources.database, resources.event_platform.publisher)
    conversations = ConversationService(
        settings, resources.database, resources.event_platform.publisher
    )
    service = MessagingService(
        settings,
        resources.database,
        resources.event_platform.publisher,
        vault,
        transport,
        customers,
        conversations,
    )
    resources.channels = service
    resources.channel_webhooks = InboundMessagingService(
        settings,
        resources.database,
        resources.event_platform.publisher,
        vault,
        customers,
        conversations,
        service,
    )
    return transport


async def _token(auth_client: Any, make_auth_user: Any, org: Any, role: RoleKey) -> str:
    email, password, _ = await make_auth_user(organization=org, role=role)
    login = await auth_client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert login.status_code == 200, login.text
    return str(login.json()["access_token"])


@pytest.fixture
async def api(auth_client: Any, make_auth_org: Any, make_auth_user: Any) -> Any:
    transport = _rewire(auth_client)
    org = await make_auth_org()
    owner = await _token(auth_client, make_auth_user, org, RoleKey.ORG_OWNER)
    auth = {"Authorization": f"Bearer {owner}"}

    class _Api:
        client = auth_client
        organization = org
        transport_ = transport

        async def request(self, method: str, path: str, **kw: Any) -> Any:
            return await auth_client.request(
                method, f"/api/v1{path}", headers={**auth, **kw.pop("headers", {})}, **kw
            )

        async def post(self, path: str, body: Any) -> Any:
            return await self.request("POST", path, json=body)

        async def get(self, path: str) -> Any:
            return await self.request("GET", path)

        async def patch(self, path: str, body: Any) -> Any:
            return await self.request("PATCH", path, json=body)

        async def delete(self, path: str) -> Any:
            return await self.request("DELETE", path)

    return _Api()


async def test_account_and_message_lifecycle_through_http(api: Any) -> None:
    created = await api.post(
        "/messaging/accounts",
        {
            "channel": "SMS",
            "provider": "generic_http",
            "slug": f"sms-{uuid.uuid4().hex[:8]}",
            "external_account_id": uuid.uuid4().hex,
            "sender_identity": "+14155550100",
        },
    )
    assert created.status_code == 201, created.text
    account = created.json()
    assert account["sender_identity"] == "+14155550100"
    assert account["receive_path"].startswith("/api/v1/webhooks/messaging/generic_http/")

    assert (await api.get("/messaging/accounts")).json()[0]["id"] == account["id"]
    stored = await api.post(
        f"/messaging/accounts/{account['id']}/credentials",
        {"fields": {"api_token": "tok", "webhook_secret": "whsec"}},
    )
    assert stored.status_code == 204
    assert (await api.get(f"/messaging/accounts/{account['id']}")).json()["has_credential"]

    # a conversation to attach the outbound message to
    from nexus_ai.domain.customers.entities import CreateConversationRequest

    resources = api.client.nexus_app.state.lifespan.resources
    conversation, _ = await resources.conversations.open_or_resolve(
        api.organization.id, CreateConversationRequest(channel="sms")
    )

    sent = await api.post(
        "/messaging/messages",
        {
            "account_id": account["id"],
            "conversation_id": str(conversation.id),
            "to": [{"value": "+14155550142"}],
            "content": {"content_type": "TEXT", "text": "hello via http"},
        },
    )
    assert sent.status_code == 201, sent.text
    message = sent.json()
    assert message["status"] == "SENT" and message["direction"] == "OUTBOUND"
    assert "token" not in json.dumps(message)
    assert api.transport_.requests and api.transport_.requests[-1]["headers"]["Authorization"]

    fetched = await api.get(f"/messaging/messages/{message['id']}")
    assert fetched.status_code == 200
    listed = await api.get(f"/messaging/messages?conversation_id={conversation.id}")
    assert listed.json()[0]["id"] == message["id"]

    disabled = await api.post(f"/messaging/accounts/{account['id']}/disable", {})
    assert disabled.json()["status"] == "DISABLED"
    # an account with message history is not hard-deletable (409); a fresh one is (204)
    assert (await api.delete(f"/messaging/accounts/{account['id']}")).status_code == 409
    fresh = await api.post(
        "/messaging/accounts",
        {
            "channel": "SMS",
            "provider": "generic_http",
            "slug": f"sms-{uuid.uuid4().hex[:8]}",
            "external_account_id": uuid.uuid4().hex,
            "sender_identity": "+14155550100",
        },
    )
    assert (await api.delete(f"/messaging/accounts/{fresh.json()['id']}")).status_code == 204


async def test_inbound_webhook_route_verifies_signature(api: Any) -> None:
    created = await api.post(
        "/messaging/accounts",
        {
            "channel": "SMS",
            "provider": "generic_http",
            "slug": f"sms-{uuid.uuid4().hex[:8]}",
            "external_account_id": uuid.uuid4().hex,
            "sender_identity": "+14155550100",
        },
    )
    account = created.json()
    await api.post(
        f"/messaging/accounts/{account['id']}/credentials",
        {"fields": {"webhook_secret": "whsec"}},
    )
    token = account["receive_path"].rsplit("/", 1)[-1]
    body = json.dumps(
        {"from": "+14155550142", "to": "+14155550100", "message_id": "in-1", "text": "hi"}
    ).encode()
    sig = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()

    ok = await api.client.post(
        f"/api/v1/webhooks/messaging/generic_http/{token}",
        headers={"X-Messaging-Signature": f"sha256={sig}", "Content-Type": "application/json"},
        content=body,
    )
    assert ok.status_code == 202, ok.text
    assert ok.json()["received"] == 1

    bad = await api.client.post(
        f"/api/v1/webhooks/messaging/generic_http/{token}",
        headers={"X-Messaging-Signature": "sha256=00", "Content-Type": "application/json"},
        content=body,
    )
    assert bad.status_code == 401


async def test_member_can_send_but_not_manage_accounts(
    api: Any, auth_client: Any, make_auth_user: Any
) -> None:
    member = await _token(auth_client, make_auth_user, api.organization, RoleKey.ORG_MEMBER)
    headers = {"Authorization": f"Bearer {member}"}
    denied = await auth_client.post(
        "/api/v1/messaging/accounts",
        headers=headers,
        json={
            "channel": "SMS",
            "provider": "generic_http",
            "slug": f"x-{uuid.uuid4().hex[:8]}",
            "external_account_id": uuid.uuid4().hex,
            "sender_identity": "+14155550100",
        },
    )
    assert denied.status_code == 403
    # a member may still send (to a ghost account -> 404, not 403)
    ghost = await auth_client.post(
        "/api/v1/messaging/messages",
        headers=headers,
        json={
            "account_id": "00000000-0000-7000-8000-000000000000",
            "conversation_id": "00000000-0000-7000-8000-000000000001",
            "to": [{"value": "+14155550142"}],
            "content": {"content_type": "TEXT", "text": "hi"},
        },
    )
    assert ghost.status_code == 404, ghost.text
