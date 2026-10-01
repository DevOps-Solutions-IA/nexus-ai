# H0-01 governance and lifecycle hardening

Authority: GitHub Issue #51 and its H0-00 authorization comment. Baseline main is
`d23f124f0a1a94d0cefe37e0fc8e5f9f17ea2b6e`. This work is on
`chore/nxs-h0-governance-hardening`, pending external audit and merge. It does not
change product phase status, implement future product features or authorize deployment.

## Admission

Registry order breaks ties between dependency-qualified phases. A normal start must
select that deterministic head, even when a later phase has individually satisfied
dependencies. Existing active-phase guard checks still apply; BUILDING continuation is
a no-op only after those checks. Controlled READY → VALIDATING reopening remains the
canonical corrective mechanism. H0 does not reopen P21 or operate P22's lifecycle.

## Future mandatory roadmap requirements

| Owner | Requirement IDs | Required future proof |
|---|---|---|
| P23 | NXS-METER-001, NXS-COST-001 | Durable usage; deterministic version-bound cost attribution |
| P25 | NXS-WF-002, NXS-SCHED-002, NXS-HUMAN-002, NXS-SRE-002, NXS-RES-003 | Orphan/stale claim recovery, bounded reconciliation, stale-owner fences and stable operation identities |
| P27 | NXS-SEC-004, NXS-SUPPLY-001 | Production isolation/security, cross-phase semantic synchronization, SIP root cause and fresh supply-chain certification |
| P28 | NXS-CAP-002, NXS-CAP-003 | Measured capacity envelopes and bounded saturation/backpressure |
| P29 | NXS-CHAOS-001, NXS-FAILOVER-001 | Controlled fault injection and evidenced failover/recovery |
| P30 | NXS-CERT-001, NXS-CERT-002 | Immutable backend certification with completed audit authority and no unresolved production blockers |
| P31 | NXS-RELEASE-001 | Reproducible immutable release with provenance and rollback documentation |
| P32 | NXS-DEPLOY-001, NXS-DEPLOY-002 | Explicitly authorized deployment, verification and rollback |

These are PLANNED mandatory obligations only. Existing AGENT-002, SEC-002, CAP-001 and
all historical mappings remain intact. P30 now directly depends on P22 as well as
P27/P29; safety is enforced by DAG and machine-readable certification policy, not just
registry order. New manifests are created through ordinary admission when authorized.

## Durable deferrals

The registry tracks P13/P14/P15/P17/P20 recovery gaps under P25 and the cross-phase
concurrency, supply-chain and SIP findings under P27. All currently remain OPEN and
block P30 certification. No historical phase is retroactively marked failed by these
future obligations. Updating a record must retain a legal status history; resolution
needs owning-phase closure evidence and the owning implementation commit present in Git.
Reopening an owner with resolved obligations requires reopening those obligations too.

## H0-00 security precondition

Baseline dependency audit failed: PyJWT 2.13.0 and urllib3 2.7.0. Authorized correction
uses PyJWT 2.15.0 and urllib3 2.8.0 only. TokenService retains EdDSA, trusted local kid,
token type and issuer/audience/required/time-claim validation. Dependency regression
tests cover option mapping preservation and malformed JWK-set behavior; the latter is
library coverage, not a new Nexus JWK authentication path. Historical failure remains
separate from fresh certification.

## Preservation and review

P22 candidate `890ff6caf6b3d23357a0e3f586476f0c8d0613c3` and PR #50 remain frozen.
H0 adds no P22 runtime/schema/test changes. No nxs-gate, nxs-close, merge, P23 start,
deployment or branch-protection mutation is authorized. A draft H0 PR with actual
candidate CI/Security results is the review boundary.
