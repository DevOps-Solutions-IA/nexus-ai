# ADR-0067: Tool risk and side-effect classification

Status: Accepted. Context: NXS-TOOL-001 requires explicit, bounded risk classes and a side-effect classification, and forbids inventing unsafe autonomous approval behaviour.

Decision:

**Risk class is a bounded ordinal.** `RiskClass` is `LOW < MEDIUM < HIGH < CRITICAL`. Every tool declares one (default `MEDIUM`). The organization policy carries a ceiling, `settings.tools.max_risk_class` (default `HIGH`). Registration, update and activation all reject a tool whose risk exceeds the ceiling (`NXS_TOOL_POLICY_DENIED` at register/update, enforced again at activate). There is no approval queue, no human-in-the-loop escalation and no "run anyway" override in P08 — a tool above the ceiling simply cannot be created or invoked in that organization.

**Side-effect class drives idempotency requirements.** `SideEffectClass` is `READ_ONLY`, `IDEMPOTENT_WRITE`, `NON_IDEMPOTENT_WRITE`, `EXTERNAL_EFFECT`. `SIDE_EFFECTING = {NON_IDEMPOTENT_WRITE, EXTERNAL_EFFECT}`. A `SIDE_EFFECTING` tool may not declare `idempotency_policy = NONE` — the registry rejects that combination at register and update time, and re-checks coherence when either field changes. When such a tool's policy is `REQUIRED`, an invocation with no `idempotency_key` is refused before any external call (ADR-0064).

**Retry safety is inherited from P07, not re-decided.** Whether a failed downstream call is retried is the Integration Hub operation's `retry_class` (ADR-0060). The Tool Engine adds no retry loop of its own. A `NON_IDEMPOTENT` operation that returns 5xx is called exactly once and surfaces `NXS_TOOL_DOWNSTREAM_ERROR`; the execution record stores `("DOWNSTREAM_ERROR", <downstream_code>)`. This is proven by resilience tests for timeout → `NXS_TOOL_TIMEOUT`, 429 → `NXS_TOOL_RATE_LIMITED`, non-idempotent 5xx not retried, and circuit-open propagation with no upstream hit.

**Both classes are versioned attributes.** Changing `risk_class` or `side_effect_class` bumps the tool `version` (ADR-0062), so an execution record always attributes an invocation to the classification in force at the time.

Consequences: an operator has one dial (the ceiling) to bound the AI plane's blast radius per organization; the idempotency requirement for dangerous tools is structural, not advisory; retry semantics stay in one place (P07).
