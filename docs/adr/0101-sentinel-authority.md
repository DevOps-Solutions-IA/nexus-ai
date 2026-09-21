# ADR-0101: NXS Sentinel authority, approvals and bounded SRE execution

Status: governance contract for NXS-P20. Implementation has not started.

## Context

Nexus already has durable event semantics (P04), a governed Tool Engine (P08), a provider-neutral AI Agent Runtime (P13), and PostgreSQL-authoritative Cell placement (P18). P20 introduces NXS Sentinel, an internal SRE control plane that can observe platform health, correlate operational incidents, assist diagnosis and drive narrowly governed remediation workflows without creating a second source of truth or a privileged "AI root user".

P20 precedes the dedicated compliance, audit, cost, observability, resilience, disaster-recovery, hardening, capacity, chaos and production-deployment phases. Sentinel therefore must be useful while remaining explicitly bounded by those later authorities.

## Decision

### Sentinel is an orchestrator, never subsystem authority

PostgreSQL is authoritative only for Sentinel-owned state: signal receipts, incident correlation, findings, runbook revisions, action proposals, approval decisions, execution leases/fences and execution receipts. Existing domains retain their authority:

- P04 owns durable event/outbox semantics.
- P08 owns tenant-scoped controlled Tool Engine execution; P20 does not execute tenant business mutations and never repurposes P08 as a global SRE executor.
- P13 owns tenant-scoped agent/session reasoning. P20 may reuse its provider-neutral adapter contracts, never its tenant persistence as platform authority.
- P18 owns Organization-to-Cell placement.
- P11/P19 retain telephony/SIP authority.
- Git/NXS lifecycle remains the engineering-control authority.

Signals, caches, NATS deliveries, LLM output and process memory never become action authority.

### Trust boundary

Sentinel runs under a dedicated internal platform identity. It is not a tenant principal and does not inherit Organization-owner permissions. Organization IDs present in signals are subjects being diagnosed, not caller-supplied authority. P20 does not create tenant-scoped diagnostic reads as a shortcut around RLS; an Organization may be named as an affected subject only from trusted platform facts or from an existing service API that already enforces its own authority.

Sentinel-owned tables use a dedicated PostgreSQL login role `nexus_sentinel`: LOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOBYPASSRLS, NOREPLICATION, no schema CREATE, and explicit grants only on Sentinel-owned tables/sequences. `nexus_sentinel` does not receive blanket default privileges on tenant tables. Migration remains owned by `nexus_migration`. The normal `nexus_runtime` role is not used as Sentinel's durable-state authority.

The model sees sanitized structured evidence only. It never receives provider credentials, database credentials, SSH keys, raw secret-bearing environment values or unrestricted logs. Sentinel model credentials are platform-scoped and resolved through a dedicated `SentinelModelCredentialProvider`; they are not stored in tenant P07/P13 vault rows. Hardened environments load them from an operations-controlled external secret reference/read-only secret mount (or an equivalent platform secret provider) and fail closed if missing or insecure.

### Signal ingestion

Sentinel accepts signals only from registered trusted adapters. Each signal has a stable source identity, source event/observation identity, observed-at time, subject kind/id, severity, bounded typed facts and sanitized evidence references. Duplicate delivery is idempotent. Untrusted payload fields cannot choose an Organization, Cell, service, runbook or action target.

P20 does not establish P24's general observability platform. Its signal adapters are deliberately bounded to operational facts required for Sentinel certification.

### Incident correlation

Correlation is deterministic and PostgreSQL-backed. A correlation key binds source class, subject identity and stable incident fingerprint. Concurrent duplicate signals converge on one incident. Absence of a signal does not by itself resolve an incident. Resolution requires an explicit trusted recovery observation or authorized operator decision.

AI-generated hypotheses are findings, not facts. Every finding records evidence references, model/provider metadata, confidence and a structured explanation. A finding cannot itself authorize execution.

### Runbook model

Sentinel runbooks are immutable, versioned platform-controlled definitions. A runbook identifies a pre-registered structured handler/tool key, typed parameter schema, allowed target kinds, risk class, preconditions, timeout, idempotency semantics, success evidence and rollback/compensation metadata where applicable.

Runbooks never contain executable shell, SQL, arbitrary URL templates or dynamically supplied code. Database records may select only handlers already registered in trusted source code. Tenant users and the language model cannot create executable handlers.

Risk classes:

1. OBSERVE — read-only health/status collection; may run autonomously under policy.
2. DIAGNOSTIC — bounded non-mutating diagnostic action; may run autonomously under policy.
3. REVERSIBLE — narrowly scoped mutation with documented compensation; requires a durable approval unless a future certified policy explicitly allows a named action. P20 ships production/hardened policy approval-required.
4. HIGH_IMPACT / DESTRUCTIVE — forbidden in P20.

Deployment, failover, infrastructure provisioning, database schema mutation, arbitrary process control, credential rotation and destructive data actions are forbidden regardless of model recommendation.

### Proposal and approval

The model may propose only a registered runbook revision plus typed parameters. Server code canonicalizes and fingerprints the proposal. The fingerprint binds incident, runbook id/revision, target kind/id, sanitized parameters, expected authority generation/revision where available, risk class and policy revision.

Approval is a durable authenticated operator decision bound to that exact fingerprint and an expiry. Changing any bound field invalidates prior approval. Rejected, expired or superseded proposals cannot execute. Approval does not grant permissions broader than the runbook and target.

### Execution fencing

Each proposal has at most one active execution. PostgreSQL stores an execution generation/fence, owner identity, DB-time lease, idempotency key, dispatch state and result receipt. Only the current lease owner may cross the external side-effect boundary. A stale owner cannot act after lease recovery.

Before dispatch, Sentinel revalidates policy, approval (when required), target identity, relevant subsystem revision/generation and runbook revision. A changed target generation or stale approval fails closed.

External actions use stable idempotency identities where supported. If an external effect may have occurred but the response is lost, execution becomes AMBIGUOUS and automatic retry stops. P20 never invents a new idempotency identity to "try again". Generic automated reconciliation/failover belongs to P25.

### Platform reasoning boundary

P13 is tenant-owned by design: its provider accounts, model profiles, agents, sessions and tool bridge require an Organization context and forced RLS. Sentinel MUST NOT create a fake "system Organization", synthesize a tenant Principal, disable RLS or reuse tenant agent rows as platform SRE authority.

P20 introduces a narrow `SentinelReasoner` boundary for platform incidents. It may reuse P13's provider-neutral model adapter/request/response contracts and hardened transport patterns, but Sentinel reasoning configuration and execution receipts are platform-control state owned by P20. The reasoner has no tool loop and cannot directly execute an action. It receives bounded sanitized evidence and returns only a schema-validated finding/proposal object.

### Action execution boundary

P08 is also tenant-owned by design. Sentinel MUST NOT use the tenant Tool Engine as a global infrastructure executor.

Platform SRE actions execute through a P20 `SentinelActionExecutor` backed only by source-registered `SentinelActionAdapter` implementations. Each adapter has a stable key/revision, typed input/output, finite target allowlist, timeout, idempotency semantics, risk classification and least-privilege credential boundary. No database row or model output can create executable code or a new destination.

P20 does not execute Organization-scoped business mutations. An incident may reference an Organization as an affected subject, but Sentinel can only diagnose and recommend tenant-level remediation for an authorized operator or later governed workflow. It cannot manufacture a tenant principal or route a platform proposal into P08.

Sentinel cannot call shell, arbitrary SQL, SSH, generic cloud APIs, Kubernetes/Nomad, GitHub mutation APIs or arbitrary HTTP destinations directly. Read-only platform adapters and mutable platform adapters share the same source-registered contract and secret-redaction rules. The P20 production/hardened adapter set contains no production deployment, failover, provisioning or destructive adapter.

### Budgets and kill switch

All loops are bounded: signals per batch, correlation work, findings per incident, proposals per incident, concurrent executions, per-action timeout and retry count. A platform kill switch can disable new Sentinel executions without altering incident observation. The kill switch and risk policy are server-controlled, not prompt-controlled.

No proposal may fan out to an unbounded set of Cells, Organizations or services. Bulk remediation is outside P20 unless each bounded target has an independent authorized execution record.

### Audit/evidence boundary

P20 persists sufficient immutable-ish operational history for correctness and phase evidence, but does not claim the P22 enterprise audit platform. P04 events may announce Sentinel state changes, but events are not the source of truth. Logs/events must contain safe IDs/reason codes and no secrets, raw credentials or unbounded tenant content.

## Failure policy

Unknown adapter, malformed signal, missing target, stale generation, policy mismatch, expired approval, missing runbook, unavailable PostgreSQL or ambiguous external effect fails closed.

A model/provider outage may reduce diagnosis quality but cannot weaken policy or allow direct execution. A P08/provider outage leaves durable state and never causes an alternate uncontrolled execution path.

## Consequences

Sentinel can autonomously observe, correlate and diagnose and can drive approved structured remediation while preserving existing authorities. It cannot become a generic root shell, deployment engine, failover controller, full observability stack or production self-healing claim.

The executable design, state machines, concurrency matrix and acceptance criteria are defined in `docs/engineering/nxs-p20-sentinel-design.md`.
