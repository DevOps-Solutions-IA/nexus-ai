"""Telephony media-session foundation events + the governed transport (NXS-P11)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from nexus_ai.telephony.entities import (
    CreateAccountRequest,
    RegisterPhoneNumberRequest,
    TelephonyProvider,
)
from nexus_ai.telephony.providers.base import TransportError
from nexus_ai.telephony.providers.registry import (
    GovernedTelephonyTransport,
    known_providers,
    resolve_provider,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_SECRET = {"webhook_secret": "tel-secret", "ari_user": "u", "ari_password": "p"}


async def _account(stack: Any, org_id: Any) -> Any:
    account = await stack.service.create_account(
        org_id,
        CreateAccountRequest(
            provider=TelephonyProvider.FAKE,
            slug=f"tel-{uuid4().hex[:8]}",
            external_account_id=uuid4().hex,
            configuration={"default_country": "1"},
        ),
    )
    await stack.service.store_account_credential(org_id, account.id, _SECRET)
    return await stack.service.get_account(org_id, account.id)


def _ctx(body: dict[str, Any]) -> Any:
    from nexus_ai.telephony.providers.base import WebhookContext

    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    digest = hmac.new(b"tel-secret", f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return WebhookContext(
        method="POST",
        headers={"X-Telephony-Signature": f"sha256={digest}", "X-Telephony-Timestamp": ts},
        query={},
        body=raw,
    )


async def test_media_session_lifecycle_and_dtmf_events(
    telephony_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account = await _account(telephony_stack, org.id)
    dialed = await telephony_stack.service.register_number(
        org.id, RegisterPhoneNumberRequest(account_id=account.id, e164="+14155550100")
    )
    provider_call = f"pc-{uuid4().hex}"
    await telephony_stack.inbound.receive(
        "fake",
        account.webhook_token,
        _ctx(
            {
                "kind": "inbound",
                "event_id": uuid4().hex,
                "call_id": provider_call,
                "from": "+14155550142",
                "to": dialed.e164,
            }
        ),
    )
    await telephony_stack.inbound.receive(
        "fake",
        account.webhook_token,
        _ctx({"event_id": uuid4().hex, "call_id": provider_call, "event": "ANSWERED"}),
    )

    # a DTMF event
    dtmf = await telephony_stack.inbound.receive(
        "fake",
        account.webhook_token,
        _ctx({"event_id": uuid4().hex, "call_id": provider_call, "event": "DTMF", "digit": "5"}),
    )
    assert dtmf.processed == "dtmf"

    # media started, then stopped
    bridge = f"br-{uuid4().hex}"
    started = await telephony_stack.inbound.receive(
        "fake",
        account.webhook_token,
        _ctx(
            {
                "event_id": uuid4().hex,
                "call_id": provider_call,
                "event": "MEDIA",
                "media_state": "ACTIVE",
                "bridge_id": bridge,
                "stream_id": "st-1",
            }
        ),
    )
    assert started.processed == "media"
    await telephony_stack.inbound.receive(
        "fake",
        account.webhook_token,
        _ctx(
            {
                "event_id": uuid4().hex,
                "call_id": provider_call,
                "event": "MEDIA",
                "media_state": "STOPPED",
                "bridge_id": bridge,
            }
        ),
    )

    async with telephony_stack.database.tenant_transaction(org.id) as tenant:
        call_id = (
            await tenant.session.execute(
                text("SELECT id FROM telephony_calls WHERE provider_call_id = :p"),
                {"p": provider_call},
            )
        ).scalar_one()
        media = (
            await tenant.session.execute(
                text("SELECT state FROM telephony_media_sessions WHERE call_id = :c"),
                {"c": call_id},
            )
        ).scalar_one()
        events = {
            r.event_type
            for r in (
                await tenant.session.execute(
                    text("SELECT event_type FROM event_outbox WHERE event_type LIKE 'telephony.%'")
                )
            ).all()
        }
    assert media == "STOPPED"
    assert "telephony.dtmf.received" in events
    assert "telephony.media.started" in events and "telephony.media.stopped" in events

    sessions = await telephony_stack.service.call_media_sessions(org.id, call_id)
    assert sessions and sessions[0].state.value == "STOPPED"


async def test_governed_transport_maps_executor_failure_to_transport_error() -> None:
    from nexus_ai.integrations.backoff import FailureKind
    from nexus_ai.integrations.executor import ExecutorFailure, RawResponse

    class _Executor:
        def __init__(self, outcome: Any) -> None:
            self._outcome = outcome

        async def send(self, request: Any) -> Any:
            if isinstance(self._outcome, Exception):
                raise self._outcome
            return self._outcome

    ok = GovernedTelephonyTransport(
        _Executor(RawResponse(200, {}, b"{}", 1, "https://x")),  # type: ignore[arg-type]
        timeout_seconds=5.0,
    )
    response = await ok.request(method="GET", url="https://x", headers={}, body=None)
    assert response.status_code == 200

    failing = GovernedTelephonyTransport(
        _Executor(ExecutorFailure(FailureKind.IN_FLIGHT, RuntimeError("read timed out"))),  # type: ignore[arg-type]
        timeout_seconds=5.0,
    )
    with pytest.raises(TransportError) as exc:
        await failing.request(method="GET", url="https://x", headers={}, body=None)
    assert exc.value.timeout is True


def test_provider_registry_lists_known_providers() -> None:
    assert known_providers() == ("asterisk", "fake")
    assert resolve_provider(TelephonyProvider.FAKE).key == "fake"
    assert resolve_provider(TelephonyProvider.ASTERISK).key == "asterisk"
