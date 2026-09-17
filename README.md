# Nexus AI — by DevOps Solutions IA

Nexus AI is a production-first, multi-tenant, omnichannel enterprise AI platform designed to coordinate AI agents, human operators, messaging channels, voice, workflows, integrations, scheduling, campaigns, observability, compliance and horizontal scaling under a governed execution model.

The platform is being built phase by phase with explicit architectural contracts, deterministic state, evidence-driven quality gates and mandatory human authorization before merges to `main` or production deployment.

## Current status

Canonical backend progress on `main`:

| Phase | Capability | Status |
|---|---|---|
| NXS-P00 | Engineering Control System | READY / GO |
| NXS-P01 | Backend Core | READY / GO |
| NXS-P02 | Multi-tenancy and Organizations | READY / GO |
| NXS-P03 | Security and Authentication | READY / GO |
| NXS-P04 | Data and Event Platform | READY / GO |
| NXS-P05 | Provisioner and Dashboard Schema | READY / GO |
| NXS-P06 | Customer Identity and Conversations | READY / GO |
| NXS-P07 | Integration Hub | READY / GO |
| NXS-P08 | Tool Engine | READY / GO |
| NXS-P09 | Messaging Channels | READY / GO |
| NXS-P10 | OTP Services | READY / GO |
| NXS-P11 | Telephony Foundation | READY / GO |
| NXS-P12 | ElevenLabs Voice | CONTRACT-CERTIFIED |
| NXS-P13 | AI Agent Runtime | READY / GO |
| NXS-P14 | Workflow Engine | READY / GO |
| NXS-P15 | Scheduler | READY / GO |
| NXS-P16 | Campaigns | READY / GO |
| NXS-P17 | Human Agent Operations | PLANNED — GOVERNANCE ALIGNMENT |

P12 is contract-certified against the platform abstraction and test suite; live-provider certification remains a separate milestone.

The canonical source of truth for execution state is `.nxs/`, not this README. Always verify the current phase registry and project state before implementation.

## Architecture

Nexus AI is organized around strict ownership boundaries:

```text
Clients / Dashboard / External Systems
                │
                ▼
            NXS Core
                │
    ┌───────────┼───────────┐
    ▼           ▼           ▼
Identity     Messaging    Telephony
P06          P09          P11
    │           │           │
    └──────┬────┴─────┬─────┘
           ▼          ▼
      Tool Engine   Voice
      P08           P12
           │          │
           └────┬─────┘
                ▼
        AI Agent Runtime
              P13
                │
                ▼
        Workflow Engine
              P14
                │
                ▼
           Scheduler
              P15
                │
                ▼
           Campaigns
              P16
```

Cross-cutting platform layers include:

- PostgreSQL as the authoritative durable state store.
- Valkey for non-authoritative acceleration and caching.
- NATS + JetStream for governed event transport.
- P04 transactional event/outbox patterns.
- Forced tenant isolation through `organization_id`, RLS and tenant-aware foreign keys.
- Provider-neutral abstractions for external integrations.
- Stable idempotency identities for replay-safe operations.
- Explicit concurrency fencing and bounded work loops.
- Evidence-driven phase closure and release readiness.

## Product capabilities

The target Nexus AI platform includes:

- Multi-tenant Organizations and tenant-isolated data.
- Authentication, authorization and RBAC.
- Unified customer identity and conversation timeline.
- WhatsApp, Email and SMS channels.
- Deterministic OTP services.
- Asterisk-based telephony foundation.
- ElevenLabs-backed voice through a provider-neutral abstraction.
- Provider-neutral AI agent runtime.
- Governed Tool Engine and Integration Hub.
- Durable workflows and scheduling.
- Consent-aware and throttled campaign orchestration.
- Human-agent operations and AI ↔ human handoff.
- Horizontal NXS Cell scaling.
- SIP edge scaling.
- NXS Sentinel constrained autonomous SRE.
- Compliance, audit, metering, observability, resilience and disaster recovery.

Capabilities are only considered available when their canonical NXS phase is closed and validated. Planned roadmap items must not be treated as implemented features.

## Core engineering principles

### Production-first

Testing, security, deterministic state, failure handling and CI are part of implementation from the first commit. Production systems are not hand-edited; deployable state must originate from an approved GitHub revision.

### PostgreSQL authority

Correctness-critical state is durable. Process-local locks, in-memory cursors, Valkey locks or broker delivery do not become the source of truth for lifecycle, ownership, claims or idempotency.

### Tenant isolation

Customer tenants are called **Organizations**. Canonical tenant ownership is represented by `organization_id`. Cross-tenant references must fail closed through trusted tenant context, RLS and tenant-aware relational constraints.

### Governed external actions

The language model never receives provider credentials. Provider access flows through governed service boundaries, credential abstractions and policy enforcement.

### Bounded execution

Bulk work, retries, materialization, scans and concurrency are bounded. Nexus AI does not rely on unbounded fan-out or implicit singleton-process behavior.

### Honest delivery semantics

The platform uses durable idempotency and deterministic replay but does not claim physical exactly-once behavior from external providers where that cannot be guaranteed.

## Repository governance

Repository state controls execution.

Every implementation agent must begin with [AGENTS.md](AGENTS.md), validate `.nxs/`, inspect dependencies and run the canonical execution guard before changing implementation code.

Key governance files:

```text
AGENTS.md
CLAUDE.md
.nxs/project-state.json
.nxs/execution-policy.json
.nxs/requirements.json
.nxs/phase-registry.json
.nxs/execution-lock.json
.nxs/phases/
.nxs/evidence/
```

Important rules:

1. No implementation phase starts unless dependencies are READY.
2. Malformed or inconsistent NXS state blocks execution.
3. Only one active governed phase is currently certified at a time.
4. Every phase must close with evidence and an explicit READY / GO or FAILED / NO-GO result.
5. No merge to `main` occurs without explicit human authorization.
6. No production deployment occurs without explicit human authorization.
7. CI success alone is not enough; semantic audit and exact-head/exact-main verification are required.
8. The README must be reviewed for synchronization whenever a material phase, architecture boundary, certification state, release state or user-visible capability changes. Canonical `.nxs/` state remains the source of truth, and branch-only work must never be presented as merged capability.

## Development stack

Primary backend stack:

- Python
- FastAPI
- Pydantic
- SQLAlchemy
- Alembic
- PostgreSQL
- Valkey
- NATS + JetStream
- Docker / Docker Compose
- Asterisk 22 LTS

Planned or later-stage infrastructure includes Kamailio, Nomad and horizontally distributed NXS Cells.

The frontend will be developed after backend certification and is expected to use Next.js.

## Local development

Bootstrap and validate the repository before starting work:

```bash
make bootstrap
make nxs-validate-repo
make validate
make up
```

Useful commands:

```bash
make down
make logs
make nxs-preflight PHASE=<phase>
make nxs-gate PHASE=<phase>
```

A governed phase is started only through the canonical lifecycle, for example:

```bash
make nxs-start PHASE=NXS-P16 ACTOR=<agent>
```

Do not run `nxs-start` until the phase is formally authorized and the current `.nxs/` state permits it.

Phase closure uses the canonical implementation commit:

```bash
make nxs-close PHASE=<phase> IMPLEMENTATION_COMMIT=<sha>
```

See:

- `docs/engineering/control-system.md`
- `docs/engineering/branch-contract.md`
- `docs/runbooks/phase-lifecycle.md`

## Execution model

Nexus AI separates concerns deliberately:

```text
WHEN     → Scheduler / P15
HOW      → Workflow Engine / P14
REASON   → AI Agent Runtime / P13
ACT      → Tool Engine / P08
MESSAGE  → Messaging / P09
CALL     → Telephony / P11
VOICE    → Voice / P12
```

P16 Campaigns extends this model by governing bulk audience execution while preserving P15 temporal authority, P14 workflow authority and P09 provider authority.

## Security model

The platform is designed around fail-closed controls:

- No raw provider secrets exposed to LLMs.
- No arbitrary shell execution through business workflows.
- No arbitrary SQL supplied by clients.
- No client-authoritative tenant switching.
- No provider bypass from higher-level orchestration layers.
- No silent retries with new idempotency identities after ambiguous external effects.
- No cross-tenant entity references.
- No unbounded campaign fan-out or uncontrolled retry storms.

Security and quality checks are executed through NXS CI and NXS Security workflows and revalidated on the exact merge SHA after authorized merges.

## Roadmap

Backend roadmap:

```text
P00 Engineering Control System
P01 Backend Core
P02 Multi-tenancy and Organizations
P03 Security and Authentication
P04 Data and Event Platform
P05 Provisioner and Dashboard Schema
P06 Customer Identity and Conversations
P07 Integration Hub
P08 Tool Engine
P09 Messaging Channels
P10 OTP Services
P11 Telephony Foundation
P12 ElevenLabs Voice
P13 AI Agent Runtime
P14 Workflow Engine
P15 Scheduler
P16 Campaigns
P17 Human Agent Operations
P18 Horizontal Cell Scaling
P19 SIP Edge Scaling
P20 NXS Sentinel
P21 Compliance Controls
P22 Audit Platform
P23 Metering and Cost
P24 Observability
P25 Resilience
P26 Backup and Disaster Recovery
P27 Security Hardening
P28 Capacity Certification
P29 Chaos and Failover Certification
P30 Backend Certification
P31 GitHub Release
P32 Production Deployment
```

Frontend development follows backend certification.

## Project maturity

Nexus AI is under active development and is not yet declared production-deployed. Canonical `main` is completed through NXS-P16. NXS-P17 remains planned; this governance alignment defines its implementation contract without activating the phase or adding runtime capability.

Do not infer production readiness, provider certification, capacity certification, failover certification or deployment status from the presence of code alone. Those claims are granted only by their corresponding NXS phases and evidence.

## Vendor

**DevOps Solutions IA**

Nexus AI is developed as an enterprise platform for governed AI + human operations across communication, automation and integration workloads.
