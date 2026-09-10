"""AI Agent Runtime stable contracts (NXS-P13): states, provider-neutral model adapter,
request/response schemas, error taxonomy, P04 events, RBAC, OpenAPI governed surface, the
Tool Engine + P12 boundaries, NO chain-of-thought persistence, NXS-P14 stays PLANNED."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from nexus_ai.agents.entities import (
    AgentSessionView,
    AgentTurnView,
    ModelProvider,
    StartAgentSessionRequest,
    SubmitTurnRequest,
)
from nexus_ai.agents.errors import AGENT_ERRORS
from nexus_ai.agents.models.base import ModelProviderAdapter
from nexus_ai.agents.models.registry import known_model_providers
from nexus_ai.agents.state_machine import AgentSessionState, AgentTurnState
from nexus_ai.core.errors import NxsError
from nexus_ai.domain.auth.rbac import PERMISSION_IDS, PermissionKey
from nexus_ai.events.registry import EVENT_REGISTRY

_ROOT = Path(__file__).resolve().parents[2]

_FORBIDDEN_COT_NAMES = (
    "chain_of_thought",
    "chainofthought",
    "reasoning_trace",
    "scratchpad",
    "internal_monologue",
    "hidden_reasoning",
)


def test_agent_and_turn_states_are_frozen() -> None:
    assert [s.value for s in AgentSessionState] == [
        "PENDING",
        "ACTIVE",
        "WAITING_TOOL",
        "RESPONDING",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
    ]
    assert [s.value for s in AgentTurnState] == [
        "PENDING",
        "RUNNING",
        "AWAITING_TOOLS",
        "FINALIZING",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
    ]
    assert [p.value for p in ModelProvider] == ["openai_compatible", "fake"]
    assert known_model_providers() == ("fake", "openai_compatible")


def test_model_adapter_contract_is_provider_neutral() -> None:
    methods = {n for n, _ in inspect.getmembers(ModelProviderAdapter, inspect.isfunction)}
    assert {"generate", "stream"} <= methods
    # the provider-neutral request / response carry no vendor field
    from nexus_ai.agents.models.base import ModelRequest, ModelResponse

    for model in (ModelRequest, ModelResponse):
        fields = {f.name for f in model.__dataclass_fields__.values()}
        for banned in ("api_key", "authorization", "base_url", "endpoint", "headers", "xi_api"):
            assert banned not in fields
    # a vendor shape lives ONLY inside its adapter module
    for module in ("base.py", "fake.py", "registry.py", "transport.py"):
        src = (_ROOT / "src" / "nexus_ai" / "agents" / "models" / module).read_text().lower()
        for vendor in ("xi-api-key", "anthropic-version", "x-goog-api-key"):
            assert vendor not in src


def test_start_and_turn_requests_have_no_model_or_prompt_surface() -> None:
    start_fields = set(StartAgentSessionRequest.model_fields)
    for banned in (
        "system_instructions",
        "system_prompt",
        "model",
        "api_key",
        "api_base",
        "endpoint",
        "temperature",
        "tools",
    ):
        assert banned not in start_fields
    assert StartAgentSessionRequest.model_config.get("extra") == "forbid"
    assert SubmitTurnRequest.model_config.get("extra") == "forbid"
    assert set(SubmitTurnRequest.model_fields) == {
        "content",
        "idempotency_key",
        "correlation_id",
    }


def test_error_taxonomy_is_stable_unique_and_rfc9457() -> None:
    codes = [e.code for e in AGENT_ERRORS]
    assert len(codes) == len(set(codes))
    for error in AGENT_ERRORS:
        assert issubclass(error, NxsError)
        assert 400 <= error.status < 600
        assert error.code.startswith("NXS_AGENT_")


def test_p04_agent_events_are_registered_and_strict() -> None:
    import nexus_ai.agents.events  # noqa: F401 - registration

    for name in (
        "agent.session.created",
        "agent.session.started",
        "agent.session.completed",
        "agent.session.failed",
        "agent.session.cancelled",
        "agent.turn.started",
        "agent.turn.completed",
        "agent.turn.failed",
        "agent.tool.requested",
        "agent.tool.completed",
        "agent.tool.failed",
        "agent.response.ready",
        "agent.usage.recorded",
    ):
        model = EVENT_REGISTRY.model_for(name, 1)
        assert model.model_config.get("extra") == "forbid"
        fields = set(model.model_fields)
        for banned in (*_FORBIDDEN_COT_NAMES, "api_key", "prompt", "response_text", "content"):
            assert banned not in fields
    # the response event carries a char count, never the body
    ready = EVENT_REGISTRY.model_for("agent.response.ready", 1)
    assert "response_char_count" in ready.model_fields
    assert "content" not in ready.model_fields and "response_text" not in ready.model_fields


def test_no_chain_of_thought_field_in_the_domain_or_schema() -> None:
    import re

    from nexus_ai.agents.entities import AgentTurn
    from nexus_ai.domain.agents.models import (
        AiAgentSessionRecord,
        AiAgentToolCallRecord,
        AiAgentTurnRecord,
    )

    # no persisted column / model field is a reasoning trace
    columns = {
        c.name.lower()
        for record in (AiAgentTurnRecord, AiAgentSessionRecord, AiAgentToolCallRecord)
        for c in record.__table__.columns
    }
    fields = {f.lower() for f in AgentTurn.model_fields}
    for banned in _FORBIDDEN_COT_NAMES:
        assert not any(banned in c for c in columns), banned
        assert not any(banned in f for f in fields), banned
    # no `banned: ...` or `banned = ...` declaration in the source (prose is fine)
    decl = re.compile(r"^\s*(?:sa\.Column\(\s*\")?(" + "|".join(_FORBIDDEN_COT_NAMES) + r")\b")
    for module in ("entities.py", "runtime.py", "service.py"):
        for line in (_ROOT / "src" / "nexus_ai" / "agents" / module).read_text().splitlines():
            assert not decl.match(line.lower()), (module, line)
    for path in (_ROOT / "migrations" / "versions").glob("*agent_runtime*.py"):
        for line in path.read_text().splitlines():
            assert not any(f'"{b}"' in line.lower() for b in _FORBIDDEN_COT_NAMES), line


def test_rbac_scopes_exist_and_member_is_not_configure() -> None:
    for key in (PermissionKey.AI_READ, PermissionKey.AI_USE, PermissionKey.AI_CONFIGURE):
        assert key in PERMISSION_IDS


def test_tool_engine_is_the_only_external_action_path() -> None:
    bridge = (_ROOT / "src" / "nexus_ai" / "agents" / "toolbridge.py").read_text()
    runtime = (_ROOT / "src" / "nexus_ai" / "agents" / "runtime.py").read_text()
    service = (_ROOT / "src" / "nexus_ai" / "agents" / "service.py").read_text()
    # the runtime and the bridge never import an integration adapter, a raw http client,
    # sql text, or a shell primitive
    for source in (bridge, runtime, service):
        for banned in (
            "import httpx",
            "import subprocess",
            "import os\n",
            "from nexus_ai.integrations.adapters",
            "from nexus_ai.integrations.rest",
            "IntegrationHubService",
            "sqlalchemy.text",
        ):
            assert banned not in source
    assert "ToolEngine" in bridge and "self._engine.invoke" in bridge


@pytest.mark.anyio
async def test_openapi_agent_surface_is_governed_only(app_client) -> None:  # type: ignore[no-untyped-def]
    schema = (await app_client.get("/openapi.json")).json()
    agent_paths = [p for p in schema["paths"] if "/agents" in p]
    assert "/api/v1/agents/sessions" in agent_paths
    assert "/api/v1/agents/sessions/{session_id}/turns" in agent_paths
    blob = json.dumps(schema)
    for banned in ("xi-api-key", "chain_of_thought", "reasoning_trace"):
        assert banned not in blob
    # the session-create + turn bodies cannot carry a model, an endpoint or a prompt
    for path in (
        "/api/v1/agents/sessions",
        "/api/v1/agents/sessions/{session_id}/turns",
    ):
        body = json.dumps(schema["paths"][path]["post"])
        for banned in ("api_key", "system_prompt", "system_instructions", "endpoint", "base_url"):
            assert banned not in body


def test_p14_stays_planned_and_p13_has_no_workflow_engine() -> None:
    registry = json.loads((_ROOT / ".nxs" / "phase-registry.json").read_text())
    phases = {p["id"]: p for p in registry["phases"]}
    assert phases["NXS-P14"]["status"] == "PLANNED"
    assert phases["NXS-P14"]["branch"] == "feat/nxs-p14-workflows"
    for path in (_ROOT / "src" / "nexus_ai" / "agents").rglob("*.py"):
        for line in path.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                for banned in ("nexus_ai.workflows", "nexus_ai.campaigns", "nexus_ai.scheduler"):
                    assert banned not in stripped, (path.name, stripped)


def test_views_carry_no_credential_or_raw_prompt() -> None:
    for view in (AgentSessionView, AgentTurnView):
        for banned in ("api_key", "system_instructions", "prompt", "endpoint", "credential"):
            assert banned not in view.model_fields
