"""Telephony Foundation HTTP API surface (NXS-P11)."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import pytest

from nexus_ai.domain.auth.rbac import RoleKey

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_SECRET = "tel-api-secret"


class _FakeTransport:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._n = 0

    async def request(self, *, method, url, headers, body, timeout_seconds=None):  # type: ignore[no-untyped-def]
        from nexus_ai.telephony.providers.base import TransportResponse

        self._n += 1
        self.requests.append({"method": method, "url": url})
        return TransportResponse(
            status_code=200, headers={}, body=json.dumps({"id": f"chan-{self._n}"}).encode()
        )


def _rewire(auth_client: Any) -> _FakeTransport:
    from cryptography.fernet import Fernet

    from nexus_ai.domain.telephony.repository import TelephonySecretStore
    from nexus_ai.integrations.credentials import LocalEncryptedVault, build_fernet
    from nexus_ai.telephony.service import TelephonyService
    from nexus_ai.telephony.webhooks import InboundTelephonyService

    resources = auth_client.nexus_app.state.lifespan.resources
    settings = resources.settings
    transport = _FakeTransport()
    vault = LocalEncryptedVault(
        TelephonySecretStore(resources.database), build_fernet([Fernet.generate_key().decode()])
    )
    resources.telephony = TelephonyService(
        settings, resources.database, resources.event_platform.publisher, vault, transport
    )
    resources.telephony_webhooks = InboundTelephonyService(
        settings, resources.database, resources.event_platform.publisher, vault
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
async def tel_api(auth_client: Any, make_auth_org: Any, make_auth_user: Any) -> Any:
    transport = _rewire(auth_client)
    org = await make_auth_org()
    owner = await _token(auth_client, make_auth_user, org, RoleKey.ORG_OWNER)
    auth = {"Authorization": f"Bearer {owner}"}

    class _Api:
        transport_ = transport
        organization = org

        async def post(self, path: str, body: Any = None) -> Any:
            return await auth_client.request(
                "POST", f"/api/v1{path}", headers=auth, json=body or {}
            )

        async def post_signed(self, path: str, payload: dict[str, Any], secret: str) -> Any:
            raw = json.dumps(payload).encode()
            ts = str(int(time.time()))
            digest = hmac.new(secret.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
            return await auth_client.request(
                "POST",
                f"/api/v1{path}",
                content=raw,
                headers={
                    "X-Telephony-Signature": f"sha256={digest}",
                    "X-Telephony-Timestamp": ts,
                    "Content-Type": "application/json",
                },
            )

        async def get(self, path: str) -> Any:
            return await auth_client.request("GET", f"/api/v1{path}", headers=auth)

        async def account(self) -> str:
            created = await self.post(
                "/telephony/accounts",
                {
                    "provider": "fake",
                    "slug": f"tel-{uuid.uuid4().hex[:8]}",
                    "external_account_id": uuid.uuid4().hex,
                    "configuration": {"default_country": "1"},
                },
            )
            assert created.status_code == 201, created.text
            account_id = created.json()["id"]
            await self.post(
                f"/telephony/accounts/{account_id}/credentials",
                {"fields": {"webhook_secret": _SECRET, "ari_user": "u", "ari_password": "p"}},
            )
            return str(account_id)

        async def number(self, account_id: str, e164: str | None = None) -> tuple[str, str]:
            value = e164 or f"+1415{uuid.uuid4().int % 10_000_000:07d}"
            created = await self.post(
                "/telephony/numbers", {"account_id": account_id, "e164": value}
            )
            assert created.status_code == 201, created.text
            number_id = created.json()["id"]
            await self.post(f"/telephony/numbers/{number_id}/verify")
            return str(number_id), value

    return _Api()


async def test_outbound_call_and_hangup_over_http(tel_api: Any) -> None:
    account_id = await tel_api.account()
    number_id, _ = await tel_api.number(account_id)

    created = await tel_api.post(
        "/telephony/calls",
        {
            "provider_account_id": account_id,
            "from_number_id": number_id,
            "destination": "+14155550199",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["direction"] == "OUTBOUND" and body["state"] == "CREATED"
    assert "ari_password" not in created.text and "authorization" not in created.text.lower()

    call_id = body["id"]
    read = await tel_api.get(f"/telephony/calls/{call_id}")
    assert read.status_code == 200

    hung = await tel_api.post(f"/telephony/calls/{call_id}/hangup", {})
    assert hung.status_code == 200
    assert hung.json()["state"] == "ENDING"


async def test_raw_from_string_is_rejected_by_the_schema(tel_api: Any) -> None:
    account_id = await tel_api.account()
    number_id, _ = await tel_api.number(account_id)
    bad = await tel_api.post(
        "/telephony/calls",
        {
            "provider_account_id": account_id,
            "from_number_id": number_id,
            "destination": "+14155550199",
            "from": "+15555550000",
        },
    )
    assert bad.status_code == 422  # extra="forbid"


async def test_unsigned_webhook_route_is_rejected(tel_api: Any) -> None:
    account_id = await tel_api.account()
    account = (await tel_api.get(f"/telephony/accounts/{account_id}")).json()
    token = account["receive_path"].rsplit("/", 1)[-1]
    unsigned = await tel_api.post(f"/webhooks/telephony/fake/{token}", {"event": "RINGING"})
    assert unsigned.status_code == 401


async def test_signed_inbound_webhook_creates_a_call(tel_api: Any) -> None:
    account_id = await tel_api.account()
    _, dialed = await tel_api.number(account_id)
    account = (await tel_api.get(f"/telephony/accounts/{account_id}")).json()
    token = account["receive_path"].rsplit("/", 1)[-1]

    payload = {
        "kind": "inbound",
        "event_id": uuid.uuid4().hex,
        "call_id": f"pc-{uuid.uuid4().hex}",
        "from": "+14155550142",
        "to": dialed,
    }
    resp = await tel_api.post_signed(f"/webhooks/telephony/fake/{token}", payload, _SECRET)
    assert resp.status_code == 202, resp.text
    assert resp.json()["accepted"] is True

    calls = await tel_api.get("/telephony/calls")
    assert any(c["direction"] == "INBOUND" for c in calls.json())


async def test_calls_require_authentication(auth_client: Any, make_auth_org: Any) -> None:
    _rewire(auth_client)
    await make_auth_org()
    anon = await auth_client.post(
        "/api/v1/telephony/calls",
        json={
            "provider_account_id": str(uuid.uuid4()),
            "from_number_id": str(uuid.uuid4()),
            "destination": "+14155550199",
        },
    )
    assert anon.status_code in (401, 403)
