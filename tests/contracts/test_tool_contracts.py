"""Tool Engine stable contracts — schemas, error taxonomy, API surface (NXS-TOOL-001)."""

from __future__ import annotations

import uuid

import pytest

from nexus_ai.core.errors import NxsError
from nexus_ai.events.registry import EVENT_REGISTRY
from nexus_ai.tools.entities import (
    ToolDefinitionView,
    ToolInvocation,
    ToolResult,
    ToolResultClass,
)
from nexus_ai.tools.errors import TOOL_ERRORS

pytestmark = pytest.mark.anyio

_FORBIDDEN_INVOCATION_FIELDS = {
    "integration_id",
    "url",
    "method",
    "headers",
    "query",
    "operation_key",
    "graphql",
    "document",
    "organization_id",
    "base_url",
}


def test_error_taxonomy_is_stable_and_unique() -> None:
    codes = [e.code for e in TOOL_ERRORS]
    assert len(codes) == len(set(codes))
    for error in TOOL_ERRORS:
        assert error.code.startswith("NXS_TOOL_")
        assert issubclass(error, NxsError)
        assert 400 <= error.status <= 599
        assert error.title and error.title != NxsError.title
    retryable = {e.code for e in TOOL_ERRORS if e.retryable}
    assert "NXS_TOOL_TIMEOUT" in retryable
    assert "NXS_TOOL_RATE_LIMITED" in retryable
    assert "NXS_TOOL_EXECUTION_IN_PROGRESS" in retryable
    assert "NXS_TOOL_PERMISSION_DENIED" not in retryable
    assert "NXS_TOOL_ARGS_INVALID" not in retryable


def test_invocation_contract_has_no_execution_primitive() -> None:
    fields = set(ToolInvocation.model_fields)
    assert fields == {"tool_key", "arguments", "idempotency_key", "correlation_id"}
    assert not (fields & _FORBIDDEN_INVOCATION_FIELDS)


def test_tool_result_and_definition_round_trip() -> None:
    result = ToolResult(
        tool_key="t.x",
        tool_version=2,
        result_class=ToolResultClass.SUCCESS,
        ok=True,
        status_code=200,
        output={"id": "c1"},
    )
    assert ToolResult.model_validate(result.model_dump(mode="json")) == result
    assert "input_schema" in ToolDefinitionView.model_fields
    assert "integration_id" in ToolDefinitionView.model_fields  # binding is visible for operators


def test_event_payloads_registered_and_strict() -> None:
    from pydantic import ValidationError

    for event_type in (
        "tools.registered",
        "tools.updated",
        "tools.disabled",
        "tools.invocation.completed",
        "tools.invocation.failed",
    ):
        assert EVENT_REGISTRY.is_known_type(event_type)
        model = EVENT_REGISTRY.model_for(event_type, 1)
        with pytest.raises(ValidationError):
            model.model_validate({"unexpected": 1})


async def test_api_has_no_arbitrary_execution_endpoint(app_client) -> None:  # type: ignore[no-untyped-def]
    schema = (await app_client.get("/openapi.json")).json()
    paths = set(schema["paths"])
    # No generic arbitrary-execution primitive: match on whole path segments so that
    # governed, bound endpoints (e.g. the P07 integration hub's ``/execute``) are not
    # false positives, while ``run-anything``/``shell``/``eval``/``exec`` primitives are.
    banned_segments = {
        "run-anything",
        "run_anything",
        "shell",
        "exec",
        "eval",
        "sql",
        "graphql",
        "proxy",
    }
    banned_substrings = ("/http/request", "run-anything", "run_anything")
    for path in paths:
        segments = {seg.strip("{}") for seg in path.split("/") if seg}
        assert not (segments & banned_segments), path
        assert not any(b in path for b in banned_substrings), path
    for expected in (
        "/api/v1/tools",
        "/api/v1/tools/{tool_id}",
        "/api/v1/tools/invoke",
    ):
        assert expected in paths, expected
    invoke = schema["paths"]["/api/v1/tools/invoke"]["post"]
    ref = invoke["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    props = set(schema["components"]["schemas"][ref.split("/")[-1]]["properties"])
    assert props == {"tool_key", "arguments", "idempotency_key", "correlation_id"}


@pytest.mark.integration
async def test_member_can_invoke_but_not_manage(auth_client, make_auth_user, make_auth_org) -> None:  # type: ignore[no-untyped-def]
    from nexus_ai.domain.auth.rbac import RoleKey

    org = await make_auth_org()
    email, password, _ = await make_auth_user(organization=org, role=RoleKey.ORG_MEMBER)
    login = await auth_client.post(
        "/api/v1/auth/login", json={"email": email, "password": password}
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    created = await auth_client.post(
        "/api/v1/tools",
        headers=headers,
        json={
            "tool_key": "x.y",
            "name": "x",
            "input_schema": {"type": "object", "additionalProperties": False},
            "binding": {"integration_id": str(uuid.uuid4()), "operation_key": "crm.get"},
        },
    )
    assert created.status_code == 403
    missing = await auth_client.post(
        "/api/v1/tools/invoke",
        headers=headers,
        json={"tool_key": "ghost.tool", "arguments": {}},
    )
    assert missing.status_code == 404, missing.text
