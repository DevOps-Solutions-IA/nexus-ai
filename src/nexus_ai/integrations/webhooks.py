"""Inbound webhook foundation (NXS-INT-001, ADR-0057).

Provider-neutral. An endpoint is identified by an unguessable ``public_token`` whose
prefix carries the (base64) organization id, so the receive path binds the tenant scope
BEFORE any RLS-scoped lookup — the request body is NEVER trusted for tenant mapping. Each
inbound request is:

* size-bounded to the endpoint's ``max_body_bytes``;
* signature-verified when a scheme is configured (HMAC-SHA256, constant-time compare,
  timestamp tolerance, secret from the vault, secret never logged) — an unsigned request
  to a signed endpoint is rejected, never treated as verified;
* replay-checked against a durable per-endpoint dedup key with a timestamp tolerance
  window;
* normalised to an ID-only event and enqueued through the P04 transactional outbox in the
  SAME transaction as the durable receipt.

This is NOT a WhatsApp / channel-provider implementation (NXS-P09+) and it never forwards
the raw payload anywhere.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from nexus_ai.core.config import Settings
from nexus_ai.core.context import current_context
from nexus_ai.core.logging import get_logger
from nexus_ai.domain.integrations.repository import (
    WebhookEndpointRepository,
    WebhookReceiptRepository,
)
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.publisher import EventPublisher
from nexus_ai.infrastructure.database import Database
from nexus_ai.integrations.credentials import VaultClient
from nexus_ai.integrations.entities import WebhookEndpoint, WebhookSignatureScheme
from nexus_ai.integrations.errors import (
    WebhookEndpointNotFoundError,
    WebhookPayloadRejectedError,
    WebhookReplayError,
    WebhookSignatureInvalidError,
)

_TOKEN_SEP = "."  # noqa: S105 - a URL token delimiter, not a secret


def build_webhook_token(organization_id: UUID) -> str:
    prefix = base64.urlsafe_b64encode(organization_id.bytes).decode("ascii").rstrip("=")
    return f"{prefix}{_TOKEN_SEP}{secrets.token_urlsafe(24)}"


def decode_webhook_token(token: str) -> UUID:
    prefix, _, rest = token.partition(_TOKEN_SEP)
    if not rest:
        raise WebhookEndpointNotFoundError("malformed webhook token")
    try:
        padded = prefix + "=" * (-len(prefix) % 4)
        return UUID(bytes=base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError) as exc:
        raise WebhookEndpointNotFoundError("malformed webhook token") from exc


@dataclass(frozen=True, slots=True)
class WebhookAcceptance:
    webhook_endpoint_id: UUID
    integration_id: UUID
    event_type: str
    external_id: str
    replayed: bool
    normalized: dict[str, Any]


def _constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def verify_hmac_sha256(
    *,
    body: bytes,
    provided_signature: str | None,
    secret: str,
    timestamp: str | None,
    tolerance_seconds: int,
) -> None:
    if not provided_signature:
        raise WebhookSignatureInvalidError("the request is unsigned")
    signature = provided_signature.strip()
    for prefix in ("sha256=", "hmac-sha256="):
        if signature.lower().startswith(prefix):
            signature = signature[len(prefix) :]
    if timestamp is not None:
        try:
            skew = abs(time.time() - float(timestamp))
        except ValueError as exc:
            raise WebhookSignatureInvalidError("the signature timestamp is malformed") from exc
        if skew > tolerance_seconds:
            raise WebhookReplayError("the signature timestamp is outside the tolerance window")
        signed_payload = f"{timestamp}.".encode() + body
    else:
        signed_payload = body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    if not _constant_time_equal(expected, signature.lower()):
        raise WebhookSignatureInvalidError("the signature did not verify")


class InboundWebhookService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        publisher: EventPublisher,
        vault: VaultClient,
    ) -> None:
        self._settings = settings
        self._db = database
        self._publisher = publisher
        self._vault = vault
        self._log = get_logger("nexus_ai.integrations.webhooks")

    async def receive(
        self, token: str, headers: Mapping[str, str], body: bytes
    ) -> WebhookAcceptance:
        organization_id = decode_webhook_token(token)
        lowered = {k.lower(): v for k, v in headers.items()}

        async with self._db.tenant_transaction(organization_id) as tenant:
            endpoint = await WebhookEndpointRepository(tenant).by_public_token(token)
        if endpoint is None:
            raise WebhookEndpointNotFoundError("no webhook endpoint matches this token")

        if len(body) > endpoint.max_body_bytes:
            raise WebhookPayloadRejectedError("the webhook body exceeds the configured limit")

        await self._verify(organization_id, endpoint, lowered, body)

        external_id = self._external_id(endpoint, lowered, body)
        normalized = {
            "event_type": endpoint.event_type,
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "content_type": lowered.get("content-type", ""),
            "byte_length": len(body),
        }

        async with self._db.tenant_transaction(organization_id) as tenant:
            receipts = WebhookReceiptRepository(tenant)
            claimed = await receipts.claim(endpoint_id=endpoint.id, external_id=external_id)
            if not claimed:
                return WebhookAcceptance(
                    webhook_endpoint_id=endpoint.id,
                    integration_id=endpoint.integration_id,
                    event_type=endpoint.event_type,
                    external_id=external_id,
                    replayed=True,
                    normalized=normalized,
                )
            await receipts.mark(endpoint_id=endpoint.id, external_id=external_id, status="ACCEPTED")
            ctx = current_context()
            envelope = EventEnvelope.create(
                event_type="integrations.webhook.received",
                event_version=1,
                aggregate_type="integration",
                aggregate_id=str(endpoint.integration_id),
                producer=self._settings.service_name,
                organization_id=organization_id,
                correlation_id=None if ctx is None else ctx.correlation_id,
                payload={
                    "webhook_endpoint_id": str(endpoint.id),
                    "integration_id": str(endpoint.integration_id),
                    "event_type": endpoint.event_type,
                    "external_id": external_id,
                },
            )
            await self._publisher.enqueue(tenant.session, envelope)

        return WebhookAcceptance(
            webhook_endpoint_id=endpoint.id,
            integration_id=endpoint.integration_id,
            event_type=endpoint.event_type,
            external_id=external_id,
            replayed=False,
            normalized=normalized,
        )

    async def _verify(
        self,
        organization_id: UUID,
        endpoint: WebhookEndpoint,
        headers: Mapping[str, str],
        body: bytes,
    ) -> None:
        if endpoint.signature_scheme is WebhookSignatureScheme.NONE:
            return
        if not endpoint.signature_header or not endpoint.credential_ref:  # pragma: no cover
            raise WebhookSignatureInvalidError("the endpoint is misconfigured for signatures")
        material = await self._vault.get_secret(organization_id, endpoint.credential_ref)
        provided = headers.get(endpoint.signature_header.lower())
        timestamp = (
            headers.get(endpoint.timestamp_header.lower()) if endpoint.timestamp_header else None
        )
        verify_hmac_sha256(
            body=body,
            provided_signature=provided,
            secret=material.field("secret"),
            timestamp=timestamp,
            tolerance_seconds=endpoint.tolerance_seconds,
        )

    @staticmethod
    def _external_id(endpoint: WebhookEndpoint, headers: Mapping[str, str], body: bytes) -> str:
        for candidate in ("x-nxs-webhook-id", "webhook-id", "x-request-id", "x-github-delivery"):
            value = headers.get(candidate)
            if value:
                return f"hdr:{candidate}:{value[:150]}"
        return f"body:{hashlib.sha256(body).hexdigest()}"
