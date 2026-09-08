"""Inbound webhook foundation (NXS-INT-001, ADR-0057)."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.integrations.credentials import CredentialType
from nexus_ai.integrations.entities import (
    CreateIntegrationRequest,
    IntegrationStatus,
    IntegrationType,
    WebhookSignatureScheme,
)
from nexus_ai.integrations.errors import (
    WebhookEndpointNotFoundError,
    WebhookPayloadRejectedError,
    WebhookReplayError,
    WebhookSignatureInvalidError,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _endpoint(hub: Any, org_id: Any, mock_url: str, *, signed: bool) -> Any:
    integration = await hub.registry.create(
        org_id,
        CreateIntegrationRequest(
            slug="wh-int",
            name="Webhook Integration",
            integration_type=IntegrationType.WEBHOOK,
            base_url=mock_url,
        ),
    )
    await hub.registry.set_status(org_id, integration.id, IntegrationStatus.ACTIVE)
    credential_ref = None
    if signed:
        credential_ref = "wh:secret"
        await hub.registry.store_credential(
            org_id,
            credential_ref=credential_ref,
            credential_type=CredentialType.HMAC_SECRET,
            fields={"secret": "whsec_test"},
        )
    return await hub.registry.register_webhook(
        org_id,
        integration.id,
        slug="orders",
        event_type="orders.updated",
        signature_scheme=(
            WebhookSignatureScheme.HMAC_SHA256 if signed else WebhookSignatureScheme.NONE
        ),
        signature_header="X-Signature" if signed else None,
        timestamp_header="X-Timestamp" if signed else None,
        tolerance_seconds=300,
        credential_ref=credential_ref,
    )


async def test_unsigned_endpoint_accepts_and_dedups(
    integration_hub: Any, make_organization: Any, mock_http_server: Any, tenant_database: Any
) -> None:
    org = await make_organization()
    hub = integration_hub
    endpoint = await _endpoint(hub, org.id, mock_http_server.base_url, signed=False)
    body = b'{"order": 1}'
    headers = {"X-Nxs-Webhook-Id": "evt-1", "Content-Type": "application/json"}

    first = await hub.webhooks.receive(endpoint.public_token, headers, body)
    assert not first.replayed and first.event_type == "orders.updated"
    second = await hub.webhooks.receive(endpoint.public_token, headers, body)
    assert second.replayed

    async with tenant_database.tenant_transaction(org.id) as tenant:
        receipts = (
            await tenant.session.execute(text("SELECT count(*) FROM webhook_receipts"))
        ).scalar_one()
        events = (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM event_outbox "
                    "WHERE event_type = 'integrations.webhook.received'"
                )
            )
        ).scalar_one()
    assert receipts == 1 and events == 1


async def test_signed_endpoint_verifies(
    integration_hub: Any, make_organization: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    hub = integration_hub
    endpoint = await _endpoint(hub, org.id, mock_http_server.base_url, signed=True)
    body = b'{"order": 2}'
    ts = str(int(time.time()))
    good = hmac.new(b"whsec_test", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()

    accepted = await hub.webhooks.receive(
        endpoint.public_token,
        {"X-Signature": f"sha256={good}", "X-Timestamp": ts, "X-Nxs-Webhook-Id": "evt-2"},
        body,
    )
    assert not accepted.replayed

    with pytest.raises(WebhookSignatureInvalidError):
        await hub.webhooks.receive(
            endpoint.public_token,
            {"X-Signature": "sha256=deadbeef", "X-Timestamp": ts, "X-Nxs-Webhook-Id": "evt-3"},
            body,
        )
    with pytest.raises(WebhookSignatureInvalidError, match="unsigned"):
        await hub.webhooks.receive(
            endpoint.public_token, {"X-Timestamp": ts, "X-Nxs-Webhook-Id": "evt-4"}, body
        )


async def test_signed_endpoint_rejects_stale_timestamp(
    integration_hub: Any, make_organization: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    hub = integration_hub
    endpoint = await _endpoint(hub, org.id, mock_http_server.base_url, signed=True)
    body = b"{}"
    stale = str(int(time.time()) - 9999)
    sig = hmac.new(b"whsec_test", f"{stale}.".encode() + body, hashlib.sha256).hexdigest()
    with pytest.raises(WebhookReplayError):
        await hub.webhooks.receive(
            endpoint.public_token,
            {"X-Signature": f"sha256={sig}", "X-Timestamp": stale, "X-Nxs-Webhook-Id": "e"},
            body,
        )


async def test_body_size_limit(
    integration_hub: Any, make_organization: Any, mock_http_server: Any
) -> None:
    org = await make_organization()
    hub = integration_hub
    endpoint = await _endpoint(hub, org.id, mock_http_server.base_url, signed=False)
    hub.settings.integrations  # noqa: B018 - readability
    huge = b"x" * (hub.settings.integrations.webhook_max_body_bytes + 1)
    with pytest.raises(WebhookPayloadRejectedError):
        await hub.webhooks.receive(endpoint.public_token, {}, huge)


async def test_bad_token_is_not_found(integration_hub: Any) -> None:
    with pytest.raises(WebhookEndpointNotFoundError):
        await integration_hub.webhooks.receive("not-a-real-token", {}, b"{}")
