# ADR-0090: AI Agent Runtime architecture and phase boundaries

Status: Accepted — NXS-P13 (`NXS-AGENT-001`), 2026-09-10.

## Context

Nexus needs a permanent "brain execution layer": the component that runs a configured
agent against a language model, lets the model ask for external actions, executes those
actions under Nexus authority, and returns a channel-neutral response. Voice transport
(NXS-P12), telephony (NXS-P11), the external Tool Engine (NXS-P08) and durable workflow
orchestration (NXS-P14, still `PLANNED`) already have — or will have — their own phases.
P13 must slot between them without absorbing their responsibilities.

## Decision

Introduce `nexus_ai.agents` — a tenant-isolated, provider-neutral agent runtime.

**What P13 owns**

* Agent, Model Profile and Model Provider Account definitions (`ai_agents`,
  `ai_model_profiles`, `ai_model_provider_accounts`).
* Agent session and turn lifecycle (`ai_agent_sessions`, `ai_agent_turns`) — see
  ADR-0093.
* Model invocation through a provider-neutral adapter (ADR-0091).
* Deterministic context assembly and layered prompt governance (ADR-0092).
* A bounded tool-execution loop that calls **only** the NXS-P08 Tool Engine (ADR-0093).
* Cancellation, concurrency and idempotency (ADR-0094).
* Token/usage accounting (`ai_model_usage`) with no pricing and no billing.
* `agent.*` outbox events (NXS-EVENT 013) carrying only externally meaningful facts.

**What P13 does NOT own**

Telephony, SIP, ElevenLabs / any realtime voice transport, direct CRM–ERP integrations,
provider credentials storage (delegated to the NXS-P07 vault), durable/multi-step
workflows, campaigns, schedulers, human-agent operations, production autoscaling, and the
Sentinel supervisory layer. P13 imports nothing from `nexus_ai.workflows`,
`nexus_ai.campaigns` or `nexus_ai.scheduler` (contract-tested).

**Boundary summary**

| Phase | Authority |
|-------|-----------|
| NXS-P08 | The only path for external action execution (tools) |
| NXS-P11 | Telephony / call control |
| NXS-P12 | Realtime voice transport & provider media |
| **NXS-P13** | **AI reasoning / runtime orchestration** |
| NXS-P14 | Durable workflow orchestration (PLANNED) |

## Consequences

* A voice call (P12) or an inbound message (P09) feeds normalized user text into a P13
  session via a channel-neutral `AgentInput`; P13 never sees a provider media frame.
* The runtime holds no ambient authority: every external effect is a Tool Engine
  invocation authorized by server-side RBAC + the agent's allow-list, never by the
  model's request (ADR-0093).
* Adding NXS-P14 later means orchestrating P13 turns from a durable engine — P13 needs no
  change because a turn is already an idempotent, cancellable, self-contained unit.
* The model is treated as an untrusted input source end to end (ADR-0091, ADR-0092).
