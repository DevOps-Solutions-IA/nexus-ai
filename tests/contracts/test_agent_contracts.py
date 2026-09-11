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
        "EXPIRED",
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
    # audit corrective #2: the whole-turn deadline and the session lifetime ceiling are
    # distinct stable members — a bare TimeoutError must never reach a caller.
    from nexus_ai.agents.errors import (
        AgentProviderTimeoutError,
        AgentSessionExpiredError,
        AgentTurnTimeoutError,
    )

    by_code = {e.code: e for e in AGENT_ERRORS}
    assert by_code["NXS_AGENT_TURN_TIMEOUT"] is AgentTurnTimeoutError
    assert AgentTurnTimeoutError.status == 504
    assert by_code["NXS_AGENT_SESSION_EXPIRED"] is AgentSessionExpiredError
    assert AgentSessionExpiredError.status == 409
    assert AgentProviderTimeoutError.code == "NXS_AGENT_PROVIDER_TIMEOUT"  # still distinct

    service_src = (_ROOT / "src" / "nexus_ai" / "agents" / "service.py").read_text()
    # the per-Agent deadline and the absolute lifetime are enforced, and the total-turn
    # deadline raises the stable error rather than re-raising the builtin.
    assert "_effective_turn_deadline" in service_src and "min(agent.timeout_seconds" in service_src
    assert "_lifetime_exceeded" in service_src and "max_session_seconds" in service_src
    assert "raise AgentTurnTimeoutError(" in service_src
    timeout_block = service_src.split("except TimeoutError:", 1)[1].split("except asyncio", 1)[0]
    assert "\n                raise\n" not in timeout_block  # no bare re-raise of TimeoutError

    # audit corrective #3: terminal-state absorption at the DATABASE boundary, and the
    # whole-turn deadline is bounded by the session's remaining absolute lifetime.
    finish_body = service_src.split("async def _finish_turn", 1)[1].split("\n    async def ", 1)[0]
    assert "session_is_terminal(refreshed.state)" in finish_body  # re-checked after FOR UPDATE
    assert "_discard_stale_turn" in finish_body  # stale outcome discarded, not committed
    assert "_remaining_session_lifetime" in service_src
    submit_body = service_src.split("async def submit_turn", 1)[1].split("\n    async def ", 1)[0]
    assert "max(remaining, 0.0)" in submit_body  # effective deadline includes the lifetime
    assert "_expire_session_now" in submit_body  # mid-flight expiry terminalises EXPIRED
    assert "raise AgentSessionExpiredError(" in submit_body

    # audit corrective #4: one idempotency key == one immutable logical turn == one model
    # execution owner, decided at the DATABASE boundary; truthful stale-terminal codes.
    from nexus_ai.agents.errors import AgentIdempotentReplayError

    assert by_code["NXS_AGENT_IDEMPOTENT_REPLAY"] is AgentIdempotentReplayError
    open_body = service_src.split("async def _open_turn", 1)[1].split("\n    async def ", 1)[0]
    assert "-> tuple[AgentTurn, AgentSession, bool]" in open_body  # returns ownership
    assert "_resolve_existing_turn" in open_body
    assert "except IntegrityError" in open_body  # DB-level claim, not the process lock
    assert "if not owner:" in submit_body  # a non-owner never runs the model / tools
    resolve_body = service_src.split("def _resolve_existing_turn", 1)[1].split(
        "\n    async def ", 1
    )[0]
    assert "AgentBusyError" in resolve_body  # RUNNING -> busy, do not execute
    assert "AgentIdempotentReplayError" in resolve_body  # FAILED / CANCELLED -> no rerun
    assert "_stale_terminal_code" in service_src  # truthful, never a fabricated expiry
    assert 'or "NXS_AGENT_SESSION_EXPIRED"' not in service_src  # the false-code bug is gone

    # audit corrective #5: historical replay lookup precedes new-execution admission, and
    # a replay reconstructs the EXACT original AgentResponse from immutable facts.
    from nexus_ai.agents.entities import AgentTurn

    assert {"tool_call_count", "response_correlation_id"} <= set(AgentTurn.model_fields)
    key_lookup_at = open_body.index("by_session_idempotency_key")
    terminal_check_at = open_body.index("session_is_terminal(session.state)")
    lifetime_check_at = open_body.index("_lifetime_exceeded(session")
    assert key_lookup_at < terminal_check_at < lifetime_check_at  # ordering is locked
    response_body = service_src.split("def _response_from_turn", 1)[1].split("\n    def ", 1)[0]
    assert "turn.tool_call_count" in response_body  # not tool_iterations
    assert "turn.tool_iterations" not in response_body
    assert "turn.response_correlation_id" in response_body  # not a fabricated None
    assert "correlation_id=None" not in response_body


def test_p04_agent_events_are_registered_and_strict() -> None:
    import nexus_ai.agents.events  # noqa: F401 - registration

    for name in (
        "agent.session.created",
        "agent.session.started",
        "agent.session.completed",
        "agent.session.failed",
        "agent.session.cancelled",
        "agent.session.expired",
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


def test_tool_budgets_are_turn_wide_and_the_p08_key_is_semantic() -> None:
    """Audit corrective #1: the turn tool-call budget and the byte-exact result bound
    are turn-wide semantic contracts, not per-response artefacts."""
    from nexus_ai.core.config import AgentRuntimeSettings

    cfg = AgentRuntimeSettings()
    assert cfg.max_tool_calls_per_turn >= 1 and cfg.max_tool_calls_per_response >= 1
    assert "max_tool_calls_per_turn" in AgentRuntimeSettings.model_fields
    assert "max_tool_calls_per_response" in AgentRuntimeSettings.model_fields

    bridge = (_ROOT / "src" / "nexus_ai" / "agents" / "toolbridge.py").read_text()
    runtime = (_ROOT / "src" / "nexus_ai" / "agents" / "runtime.py").read_text()
    # the P08 idempotency key is derived from the semantic identity only — never the
    # model tool_call_id and never the loop iteration index.
    assert "_derive_key(idempotency_seed, call.name, arg_hash)" in bridge
    assert "idempotency_seed=ctx.idempotency_seed" in runtime
    assert ":{iteration}" not in runtime and "call.id, arg_hash" not in bridge
    # the semantic-dedup cache and the turn budget live OUTSIDE the iteration loop
    pre_loop = runtime.split("for iteration in range(", 1)[0]
    assert "executed: dict" in pre_loop and "tool_calls_requested = 0" in pre_loop


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


#: every "exactly once" / "exactly-once" occurrence found in the agents source tree and
#: ADR-0094 as of audit corrective #6 — narrowly scoped, verified true by two independent
#: subagents. A NEW occurrence not on this list fails the test below: it must be reviewed
#: for overclaiming (audit corrective #6, final-auditor LOW finding — claim discipline had
#: no automated regression protection) before being added here.
_ALLOWED_EXACTLY_ONCE_LINES = (
    "exactly once per distinct executed call). Persisting was chosen over deriving because (a)",
    # audit corrective #7 — a NEGATIVE claim (explicitly disclaiming an exactly-once
    # physical-effect guarantee for the crash-window P1/P2 cases), not an overclaim.
    '**No claim of "exactly-once" physical external effect** is made anywhere in this',
)


def test_exactly_once_claims_are_locked_to_the_known_narrow_scoped_set() -> None:
    """Claim discipline (audit corrective #6 §16): no system-wide 'exactly once' guarantee
    is ever claimed — only this one, narrowly scoped statement about the ``on_tool``
    callback's per-distinct-call invocation count. A future PR introducing a NEW
    unscoped 'exactly once' claim anywhere in the agents source tree or ADR-0094 fails
    this test rather than silently drifting into an overclaim."""
    targets = [
        *(_ROOT / "src" / "nexus_ai" / "agents").rglob("*.py"),
        _ROOT / "docs" / "adr" / "0094-cancellation-concurrency-and-idempotency.md",
    ]
    found: list[str] = []
    for path in targets:
        for line in path.read_text().splitlines():
            if "exactly once" in line.lower() or "exactly-once" in line.lower():
                found.append(line.strip())
    assert found, "the one known-safe occurrence is expected to still exist"
    for line in found:
        assert line in _ALLOWED_EXACTLY_ONCE_LINES, line
    assert len(found) == len(_ALLOWED_EXACTLY_ONCE_LINES)
