"""Asterisk ARI adapter — issues only ARI REST calls through the governed transport,
never a shell, dialplan or bare credential (NXS-P11, ADR-0084)."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import time
from typing import Any
from uuid import uuid4

import pytest

from nexus_ai.integrations.credentials import CredentialType, SecretMaterial
from nexus_ai.telephony.entities import AccountStatus, TelephonyAccount, TelephonyProvider
from nexus_ai.telephony.errors import (
    TelephonyConfigInvalidError,
    TelephonyProviderError,
    TelephonyProviderTimeoutError,
    TelephonyWebhookInvalidError,
    TelephonyWebhookReplayError,
)
from nexus_ai.telephony.providers.asterisk import AsteriskAdapter
from nexus_ai.telephony.providers.base import (
    OutboundCallSpec,
    TransportError,
    TransportResponse,
    WebhookContext,
)

pytestmark = pytest.mark.anyio

_SECRET = SecretMaterial(
    CredentialType.PROVIDER_SECRET_SET,
    {"ari_user": "nexus", "ari_password": "s3cr3t", "webhook_secret": "wh"},
)


class _Transport:
    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    async def request(self, *, method, url, headers, body, timeout_seconds=None):  # type: ignore[no-untyped-def]
        self.requests.append({"method": method, "url": url, "headers": dict(headers)})
        outcome = self._responses.pop(0) if self._responses else TransportResponse(200, {}, b"{}")
        if isinstance(outcome, TransportError):
            raise outcome
        return outcome


def _account(config: dict[str, Any] | None = None) -> TelephonyAccount:
    now = dt.datetime.now(dt.UTC)
    resolved = {"ari_base": "https://asterisk.internal:8088/ari"} if config is None else config
    return TelephonyAccount(
        id=uuid4(),
        organization_id=uuid4(),
        provider=TelephonyProvider.ASTERISK,
        slug="asterisk-main",
        external_account_id="ext-1",
        credential_ref="ref",
        status=AccountStatus.ACTIVE,
        configuration=resolved,
        webhook_token="tok",
        created_at=now,
        updated_at=now,
    )


def _spec(
    account: TelephonyAccount, *, kind: str = "PHONE", value: str = "+14155550199"
) -> OutboundCallSpec:
    return OutboundCallSpec(
        account=account,
        caller_id_e164="+14155550100",
        caller_display_name=None,
        destination_kind=kind,
        destination_value=value,
        correlation_id=None,
    )


async def test_create_outbound_call_issues_one_ari_channels_post() -> None:
    adapter = AsteriskAdapter()
    account = _account()
    transport = _Transport(TransportResponse(200, {}, json.dumps({"id": "chan-1"}).encode()))
    result = await adapter.create_outbound_call(_spec(account), _SECRET, transport)
    assert result.provider_call_id == "chan-1"
    assert len(transport.requests) == 1
    req = transport.requests[0]
    assert req["method"] == "POST"
    assert req["url"].startswith("https://asterisk.internal:8088/ari/channels?")
    assert req["headers"]["Authorization"].startswith("Basic ")
    # the raw password never appears in the URL
    assert "s3cr3t" not in req["url"]


async def test_missing_ari_base_is_config_invalid() -> None:
    adapter = AsteriskAdapter()
    with pytest.raises(TelephonyConfigInvalidError):
        await adapter.create_outbound_call(_spec(_account({})), _SECRET, _Transport())


async def test_provider_5xx_and_timeout_map_to_the_taxonomy() -> None:
    adapter = AsteriskAdapter()
    account = _account()
    with pytest.raises(TelephonyProviderError):
        await adapter.create_outbound_call(
            _spec(account), _SECRET, _Transport(TransportResponse(503, {}, b"{}"))
        )
    with pytest.raises(TelephonyProviderTimeoutError):
        await adapter.create_outbound_call(
            _spec(account),
            _SECRET,
            _Transport(TransportError("timed out", timeout=True)),
        )


async def test_hangup_treats_404_as_success_and_dtmf_posts() -> None:
    adapter = AsteriskAdapter()
    account = _account()
    await adapter.hangup_call(
        account, "chan-1", _SECRET, _Transport(TransportResponse(404, {}, b"{}"))
    )
    transport = _Transport(TransportResponse(204, {}, b""))
    await adapter.send_dtmf(account, "chan-1", "12*#", _SECRET, transport)
    assert transport.requests[0]["url"].endswith("/dtmf?dtmf=12%2A%23")


async def test_missing_ari_credential_is_a_provider_error() -> None:
    adapter = AsteriskAdapter()
    incomplete = SecretMaterial(CredentialType.PROVIDER_SECRET_SET, {"ari_user": "x"})
    with pytest.raises(TelephonyProviderError):
        await adapter.create_outbound_call(_spec(_account()), incomplete, _Transport())


def test_verify_and_parse_webhook() -> None:
    adapter = AsteriskAdapter()
    account = _account()
    body = json.dumps({"event_id": "e1", "call_id": "c1", "event": "RINGING"}).encode()
    ts = str(int(time.time()))
    digest = hmac.new(b"wh", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    ctx = WebhookContext(
        method="POST",
        headers={"X-Telephony-Signature": f"sha256={digest}", "X-Telephony-Timestamp": ts},
        query={},
        body=body,
    )
    adapter.verify_webhook(account, ctx, _SECRET, tolerance_seconds=300)
    parsed = adapter.parse_webhook(account, ctx)
    assert parsed.events and parsed.events[0].state is not None

    stale_ts = str(int(time.time()) - 5000)
    stale_digest = hmac.new(b"wh", f"{stale_ts}.".encode() + body, hashlib.sha256).hexdigest()
    stale = WebhookContext(
        method="POST",
        headers={
            "X-Telephony-Signature": f"sha256={stale_digest}",
            "X-Telephony-Timestamp": stale_ts,
        },
        query={},
        body=body,
    )
    with pytest.raises(TelephonyWebhookReplayError):
        adapter.verify_webhook(account, stale, _SECRET, tolerance_seconds=300)
    with pytest.raises(TelephonyWebhookInvalidError):
        adapter.verify_webhook(
            account, WebhookContext("POST", {}, {}, body), _SECRET, tolerance_seconds=300
        )
