"""Voice HTTP surface (NXS-P12) — governed operations only, real auth + RBAC."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from nexus_ai.domain.auth.rbac import RoleKey

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

_PCM = {"codec": "pcm_16000", "sample_rate": 16000, "channels": 1, "frame_ms": 20}


async def _token(auth_client: Any, make_auth_user: Any, org: Any, role: RoleKey) -> str:
    email, password, _ = await make_auth_user(organization=org, role=role)
    login = await auth_client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert login.status_code == 200, login.text
    return str(login.json()["access_token"])


@pytest.fixture
async def voice_api(auth_client: Any, make_auth_org: Any, make_auth_user: Any) -> Any:
    org = await make_auth_org()
    token = await _token(auth_client, make_auth_user, org, RoleKey.ORG_OWNER)
    auth = {"Authorization": f"Bearer {token}"}

    class _Api:
        organization = org

        async def post(self, path: str, body: Any = None) -> Any:
            return await auth_client.request(
                "POST", f"/api/v1{path}", headers=auth, json=body or {}
            )

        async def get(self, path: str) -> Any:
            return await auth_client.request("GET", f"/api/v1{path}", headers=auth)

    return _Api()


async def test_account_and_profile_crud_over_http(voice_api: Any) -> None:
    created = await voice_api.post(
        "/voice/accounts",
        {
            "provider": "fake",
            "slug": f"v-{uuid.uuid4().hex[:8]}",
            "external_account_id": uuid.uuid4().hex,
            "configuration": {"media_gateway_host": "10.1.2.3", "media_gateway_port": 40000},
        },
    )
    assert created.status_code == 201, created.text
    account_id = created.json()["id"]
    assert "credential" not in created.json()
    assert created.json()["receive_path"].startswith("/api/v1/webhooks/voice/fake/")

    cred = await voice_api.post(
        f"/voice/accounts/{account_id}/credentials",
        {"fields": {"api_key": "sk-secret", "webhook_secret": "wh"}},
    )
    assert cred.status_code == 204

    profile = await voice_api.post(
        "/voice/profiles",
        {
            "account_id": account_id,
            "slug": f"p-{uuid.uuid4().hex[:8]}",
            "display_name": "Support",
            "provider_voice_ref": "agent_1",
            "input_format": _PCM,
            "output_format": _PCM,
        },
    )
    assert profile.status_code == 201, profile.text
    for banned in ("api_key", "signed_url", "ws_url"):
        assert banned not in profile.json()


async def test_raw_provider_config_is_rejected_422(voice_api: Any) -> None:
    resp = await voice_api.post(
        "/voice/accounts",
        {
            "provider": "fake",
            "slug": f"v-{uuid.uuid4().hex[:8]}",
            "external_account_id": uuid.uuid4().hex,
            "configuration": {"ws_url": "wss://evil.example", "api_key": "x"},
        },
    )
    assert resp.status_code == 422


async def test_start_session_with_unknown_ids_is_not_found_or_forbidden(voice_api: Any) -> None:
    resp = await voice_api.post(
        "/voice/sessions",
        {
            "call_id": str(uuid.uuid4()),
            "media_session_id": str(uuid.uuid4()),
            "provider_account_id": str(uuid.uuid4()),
            "voice_profile_id": str(uuid.uuid4()),
        },
    )
    assert resp.status_code in (403, 404)
    assert resp.json()["type"].startswith("https://docs.nexus-ai.dev/errors/NXS_VOICE_")


async def test_voice_endpoints_require_auth(auth_client: Any) -> None:
    for method, path in (
        ("get", "/api/v1/voice/accounts"),
        ("get", "/api/v1/voice/sessions"),
    ):
        resp = await getattr(auth_client, method)(path)
        assert resp.status_code in (401, 403)


async def test_unsigned_voice_webhook_is_rejected_401(auth_client: Any) -> None:
    resp = await auth_client.post(
        "/api/v1/webhooks/voice/fake/sometoken", json={"type": "post_call"}
    )
    assert resp.status_code == 401
