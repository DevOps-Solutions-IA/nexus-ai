"""OTP Services HTTP API surface (NXS-P10)."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

import pytest

from nexus_ai.domain.auth.rbac import RoleKey

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


class _FakeTransport:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._n = 0

    async def request(self, *, method, url, headers, body, timeout_seconds=None):  # type: ignore[no-untyped-def]
        from nexus_ai.messaging.providers.base import TransportResponse

        self._n += 1
        self.requests.append({"url": url, "headers": dict(headers), "body": body})
        return TransportResponse(
            status_code=200,
            headers={},
            body=json.dumps(
                {"message_id": f"prov-{self._n}", "messages": [{"id": f"prov-{self._n}"}]}
            ).encode(),
        )


def _rewire(auth_client: Any) -> _FakeTransport:
    from cryptography.fernet import Fernet

    from nexus_ai.domain.customers.service import ConversationService, CustomerService
    from nexus_ai.domain.messaging.repository import MessagingSecretStore
    from nexus_ai.integrations.credentials import LocalEncryptedVault, build_fernet
    from nexus_ai.messaging.service import MessagingService
    from nexus_ai.otp.service import OtpService

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
    messaging = MessagingService(
        settings,
        resources.database,
        resources.event_platform.publisher,
        vault,
        transport,
        customers,
        conversations,
    )
    resources.channels = messaging
    resources.otp = OtpService(
        settings,
        resources.database,
        resources.event_platform.publisher,
        messaging,
        customers,
        conversations,
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
async def otp_api(auth_client: Any, make_auth_org: Any, make_auth_user: Any) -> Any:
    transport = _rewire(auth_client)
    org = await make_auth_org()
    owner = await _token(auth_client, make_auth_user, org, RoleKey.ORG_OWNER)
    auth = {"Authorization": f"Bearer {owner}"}

    class _Api:
        transport_ = transport
        organization = org

        async def post(self, path: str, body: Any) -> Any:
            return await auth_client.request("POST", f"/api/v1{path}", headers=auth, json=body)

        async def get(self, path: str) -> Any:
            return await auth_client.request("GET", f"/api/v1{path}", headers=auth)

        async def account(self) -> str:
            created = await self.post(
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
            account_id = created.json()["id"]
            await self.post(
                f"/messaging/accounts/{account_id}/credentials",
                {"fields": {"api_token": "t", "webhook_secret": "s"}},
            )
            return str(account_id)

        def last_code(self) -> str:
            payload = json.loads(self.transport_.requests[-1]["body"])
            match = re.search(r"\b(\d{6})\b", payload.get("text") or "")
            assert match is not None
            return match.group(1)

    return _Api()


async def test_issue_and_verify_over_http(otp_api: Any) -> None:
    account_id = await otp_api.account()
    issued = await otp_api.post(
        "/otp/challenges",
        {
            "purpose": "GENERIC_VERIFICATION",
            "channel": "SMS",
            "destination": "+14155550142",
            "messaging_account_id": account_id,
        },
    )
    assert issued.status_code == 201, issued.text
    body = issued.json()
    assert "code" not in body and "code_hash" not in body
    assert body["masked_destination"].endswith("0142") and "5555" not in body["masked_destination"]
    assert body["delivery"] == "SENT"

    challenge_id = body["challenge_id"]
    read = await otp_api.get(f"/otp/challenges/{challenge_id}")
    assert read.status_code == 200
    assert read.json()["status"] == "ACTIVE"
    assert "code_hash" not in read.text and "destination" not in read.json()

    code = otp_api.last_code()
    verified = await otp_api.post(f"/otp/challenges/{challenge_id}/verify", {"code": code})
    assert verified.status_code == 200, verified.text
    assert verified.json()["outcome"] == "VERIFIED"

    reused = await otp_api.post(f"/otp/challenges/{challenge_id}/verify", {"code": code})
    assert reused.status_code == 409
    assert reused.json()["type"].endswith("NXS_OTP_ALREADY_USED")


async def test_wrong_code_is_401_and_never_echoes_input(otp_api: Any) -> None:
    account_id = await otp_api.account()
    issued = await otp_api.post(
        "/otp/challenges",
        {
            "purpose": "GENERIC_VERIFICATION",
            "channel": "SMS",
            "destination": "+14155550142",
            "messaging_account_id": account_id,
        },
    )
    challenge_id = issued.json()["challenge_id"]
    bad = await otp_api.post(f"/otp/challenges/{challenge_id}/verify", {"code": "999111"})
    assert bad.status_code == 401
    assert "999111" not in bad.text


async def test_requires_authentication_and_permission(
    auth_client: Any, make_auth_org: Any, make_auth_user: Any
) -> None:
    _rewire(auth_client)
    await make_auth_org()
    anon = await auth_client.post(
        "/api/v1/otp/challenges",
        json={
            "purpose": "GENERIC_VERIFICATION",
            "channel": "SMS",
            "destination": "+14155550142",
            "messaging_account_id": str(uuid.uuid4()),
        },
    )
    assert anon.status_code in (401, 403)


async def test_unknown_challenge_read_is_404(otp_api: Any) -> None:
    missing = await otp_api.get(f"/otp/challenges/{uuid.uuid4()}")
    assert missing.status_code == 404
