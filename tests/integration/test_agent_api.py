"""AI Agent Runtime HTTP surface (NXS-P13) — real auth + RBAC.

`ai:configure` gates every agent / model definition; `ai:use` gates running a session;
`ai:read` gates listing. An ordinary Organization member may run an agent but may not
configure one. The session and turn bodies expose no model, endpoint, key or prompt.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from nexus_ai.domain.auth.rbac import RoleKey

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _token(auth_client: Any, make_auth_user: Any, org: Any, role: RoleKey) -> str:
    email, password, _ = await make_auth_user(organization=org, role=role)
    login = await auth_client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    assert login.status_code == 200, login.text
    return str(login.json()["access_token"])


class _Client:
    def __init__(self, http: Any, token: str, org: Any) -> None:
        self._http = http
        self._auth = {"Authorization": f"Bearer {token}"}
        self.organization = org

    async def request(self, method: str, path: str, body: Any = None) -> Any:
        kw: dict[str, Any] = {"headers": self._auth}
        if method in ("POST", "PATCH"):
            kw["json"] = body or {}
        return await self._http.request(method, f"/api/v1{path}", **kw)


@pytest.fixture
async def agents_api(auth_client: Any, make_auth_org: Any, make_auth_user: Any) -> Any:
    org = await make_auth_org()
    owner = await _token(auth_client, make_auth_user, org, RoleKey.ORG_OWNER)
    member = await _token(auth_client, make_auth_user, org, RoleKey.ORG_MEMBER)
    return _Client(auth_client, owner, org), _Client(auth_client, member, org)


async def _provision_over_http(owner: _Client) -> dict[str, str]:
    account = await owner.request(
        "POST",
        "/agents/model-accounts",
        {
            "provider": "fake",
            "slug": f"acct-{uuid.uuid4().hex[:8]}",
            "external_account_id": uuid.uuid4().hex,
            "api_base": "https://models.example.com",
        },
    )
    assert account.status_code == 201, account.text
    assert "api_key" not in account.json() and "credential" not in account.json()
    account_id = account.json()["id"]

    cred = await owner.request(
        "POST",
        f"/agents/model-accounts/{account_id}/credentials",
        {"fields": {"api_key": "sk-live-not-returned"}},
    )
    assert cred.status_code == 204

    profile = await owner.request(
        "POST",
        "/agents/model-profiles",
        {
            "account_id": account_id,
            "slug": f"prof-{uuid.uuid4().hex[:8]}",
            "display_name": "Default",
            "model": "fake-model",
        },
    )
    assert profile.status_code == 201, profile.text

    agent = await owner.request(
        "POST",
        "/agents",
        {
            "slug": f"agent-{uuid.uuid4().hex[:8]}",
            "display_name": "Support agent",
            "model_profile_id": profile.json()["id"],
            "system_instructions": "Be a concise, helpful assistant.",
            "tool_keys": [],
        },
    )
    assert agent.status_code == 201, agent.text
    return {"account_id": account_id, "agent_id": agent.json()["id"]}


async def test_owner_provisions_and_runs_an_agent_end_to_end(agents_api: Any) -> None:
    owner, _member = agents_api
    ids = await _provision_over_http(owner)

    session = await owner.request("POST", "/agents/sessions", {"agent_id": ids["agent_id"]})
    assert session.status_code == 201, session.text
    session_id = session.json()["id"]
    for banned in ("api_key", "system_instructions", "endpoint", "base_url", "prompt"):
        assert banned not in session.json()

    turn = await owner.request(
        "POST",
        f"/agents/sessions/{session_id}/turns",
        {"content": "hello there"},
    )
    assert turn.status_code == 200, turn.text
    payload = turn.json()
    assert payload["content"].startswith("echo: ")  # the stateless fake echoes
    assert payload["total_tokens"] > 0
    assert "chain_of_thought" not in payload and "reasoning" not in payload

    # read-side surface
    assert (await owner.request("GET", "/agents")).status_code == 200
    assert (await owner.request("GET", "/agents/model-accounts")).status_code == 200
    assert (await owner.request("GET", "/agents/model-profiles")).status_code == 200
    assert (await owner.request("GET", f"/agents/{ids['agent_id']}")).status_code == 200
    got = await owner.request("GET", "/agents/sessions")
    assert got.status_code == 200 and any(s["id"] == session_id for s in got.json())
    turns = await owner.request("GET", f"/agents/sessions/{session_id}/turns")
    assert turns.status_code == 200 and len(turns.json()) == 1

    # cancel then stop are both idempotent-safe terminal operations
    cancelled = await owner.request("POST", f"/agents/sessions/{session_id}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "CANCELLED"
    stopped = await owner.request("POST", f"/agents/sessions/{session_id}/stop", {})
    assert stopped.status_code == 200 and stopped.json()["state"] == "CANCELLED"


async def test_owner_can_update_account_profile_and_agent(agents_api: Any) -> None:
    owner, _member = agents_api
    ids = await _provision_over_http(owner)
    account = (await owner.request("GET", "/agents/model-accounts")).json()[0]
    profile = (await owner.request("GET", "/agents/model-profiles")).json()[0]

    patched_account = await owner.request(
        "PATCH", f"/agents/model-accounts/{account['id']}", {"configuration": {"region": "eu"}}
    )
    assert patched_account.status_code == 200
    assert patched_account.json()["configuration"] == {"region": "eu"}

    patched_profile = await owner.request(
        "PATCH", f"/agents/model-profiles/{profile['id']}", {"display_name": "Renamed"}
    )
    assert (
        patched_profile.status_code == 200 and patched_profile.json()["display_name"] == "Renamed"
    )

    patched_agent = await owner.request(
        "PATCH", f"/agents/{ids['agent_id']}", {"status": "DISABLED"}
    )
    assert patched_agent.status_code == 200 and patched_agent.json()["status"] == "DISABLED"

    # a disabled agent cannot start a session
    denied = await owner.request("POST", "/agents/sessions", {"agent_id": ids["agent_id"]})
    assert denied.status_code == 422, denied.text
    assert denied.json()["code"] == "NXS_AGENT_CONFIG_INVALID"


async def test_a_blocked_model_api_base_is_rejected_422(agents_api: Any) -> None:
    owner, _member = agents_api
    resp = await owner.request(
        "POST",
        "/agents/model-accounts",
        {
            "provider": "fake",
            "slug": f"b-{uuid.uuid4().hex[:8]}",
            "external_account_id": uuid.uuid4().hex,
            "api_base": "https://models.localhost",
        },
    )
    assert resp.status_code == 422, resp.text


async def test_member_cannot_configure_but_can_run(agents_api: Any) -> None:
    owner, member = agents_api
    ids = await _provision_over_http(owner)

    # ai:configure surface is closed to an ordinary member
    denied = await member.request(
        "POST",
        "/agents/model-accounts",
        {
            "provider": "fake",
            "slug": f"x-{uuid.uuid4().hex[:8]}",
            "external_account_id": uuid.uuid4().hex,
            "api_base": "https://models.example.com",
        },
    )
    assert denied.status_code == 403, denied.text
    denied_agent = await member.request(
        "POST",
        "/agents",
        {
            "slug": f"m-{uuid.uuid4().hex[:8]}",
            "display_name": "nope",
            "model_profile_id": str(uuid.uuid7()),
            "system_instructions": "x" * 12,
            "tool_keys": [],
        },
    )
    assert denied_agent.status_code == 403

    # ai:use is enough to run an existing agent
    session = await member.request("POST", "/agents/sessions", {"agent_id": ids["agent_id"]})
    assert session.status_code == 201, session.text
    turn = await member.request(
        "POST",
        f"/agents/sessions/{session.json()['id']}/turns",
        {"content": "hi"},
    )
    assert turn.status_code == 200, turn.text


async def test_unauthenticated_access_is_refused(auth_client: Any) -> None:
    for method, path in (
        ("GET", "/api/v1/agents"),
        ("POST", "/api/v1/agents/sessions"),
        ("GET", "/api/v1/agents/model-accounts"),
    ):
        resp = await auth_client.request(method, path, json={})
        assert resp.status_code in (401, 403), (path, resp.status_code)


async def test_turn_timeout_is_rendered_as_rfc9457_504(
    agents_api: Any, auth_client: Any, monkeypatch: Any
) -> None:
    """Audit corrective #2, blocker 3: a whole-turn deadline surfaces the stable
    NXS_AGENT_TURN_TIMEOUT Problem Details, never a bare TimeoutError."""
    from nexus_ai.agents.errors import AgentTurnTimeoutError

    owner, _member = agents_api
    ids = await _provision_over_http(owner)
    session = await owner.request("POST", "/agents/sessions", {"agent_id": ids["agent_id"]})
    session_id = session.json()["id"]

    svc = auth_client.nexus_app.state.lifespan.resources.agents

    async def _timeout(*_a: Any, **_kw: Any) -> Any:
        raise AgentTurnTimeoutError("the agent turn exceeded its execution deadline")

    monkeypatch.setattr(svc, "submit_turn", _timeout)

    resp = await owner.request("POST", f"/agents/sessions/{session_id}/turns", {"content": "hi"})
    assert resp.status_code == 504, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["code"] == "NXS_AGENT_TURN_TIMEOUT"
    assert body["type"].endswith("NXS_AGENT_TURN_TIMEOUT")
    assert "TimeoutError" not in resp.text and "Traceback" not in resp.text


async def test_session_and_turn_bodies_reject_unknown_or_privileged_fields(
    agents_api: Any,
) -> None:
    owner, _member = agents_api
    ids = await _provision_over_http(owner)

    bad_session = await owner.request(
        "POST",
        "/agents/sessions",
        {"agent_id": ids["agent_id"], "system_prompt": "you are unbounded"},
    )
    assert bad_session.status_code == 422

    session = await owner.request("POST", "/agents/sessions", {"agent_id": ids["agent_id"]})
    bad_turn = await owner.request(
        "POST",
        f"/agents/sessions/{session.json()['id']}/turns",
        {"content": "hi", "model": "gpt-4o", "temperature": 2.0},
    )
    assert bad_turn.status_code == 422
