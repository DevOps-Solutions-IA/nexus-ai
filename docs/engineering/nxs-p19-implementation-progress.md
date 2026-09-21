# P19 implementation candidate and corrective status

## Current state — pre-ARI admission corrective

The externally audited transport candidate is
`f4f6df35896e9a9f9e2bd89198fbfec5d0e894b6`; its exact-head CI 35549728454 and
Security 35549728452 passed. External review then required elimination of the total
database-outage window between committed CREATED and permit issuance. The current
corrective adds durable pre-provider owner fencing, bounded recovery and A01–A07
evidence. P19 remains BUILDING/PENDING. Closure, merge, deployment and P20 are not
authorized. Consult live lock status; no historical expiry is current authority.

The selected internal strategy is a durable PENDING owner record atomically committed
with the call/event, followed by a single irreversible DISPATCHED CAS before ARI.
Recovery revokes expired PENDING owners and their unconsumed permit slots; it never
reclaims DISPATCHED or retries an uncertain external effect. See the runtime contract
for the bounded tenant-scoped recovery operation and non-idempotent call behavior.

## HISTORICAL — transport corrective and certification

P19 remains BUILDING/PENDING on `feat/nxs-p19-sip-scaling`; canonical main remains
through P18. The complete original candidate `471b2a539bc5cb102ab5bbbe99d7d830ab83d772`
passed its 2140-test regression, original C/AC mapping, clean-room and exact-head
CI/Security. External review then identified the control-plane UDP/TCP/TLS versus
UDP-only reference-edge mismatch. Those original results are historical, not proof
for changed source. This corrective implements the approved three-transport model,
native TLS peer observation, immutable downstream pins and actual protocol tests.
The separate P11 audit fixes pre-ARI permit denial to persist FAILED, preserving
the logical call/idempotency identity and issuing no ARI request.

Current results are source-bound in `.nxs/evidence/NXS-P19/`, including supplemental
T01–T10 nodes. The final corrective candidate requires independent external review;
neither local tests nor GitHub success declares READY/GO. Closure, merge, deployment
and P20 start are not authorized. No phase restart occurred. Canonical recovery of
the expired lock and reacquisition occurred on 2026-09-20 at 19:56:26Z; consult
`make nxs-lock-status` rather than treating any documented expiry as current truth.

Transport development failures are retained in corrective evidence: native config
parameter mismatch, OpenSSL build/runtime version mismatch, strict test-PKI extension
failure, and the upstream TLS connection callback's missed transaction sends. The
last failure required a narrow, versioned source patch; a wrong-target-pin wire test
must prove zero transmitted SIP, not merely a later error response.

The first complete corrective execution passed 2245 tests with 90.76% coverage
in 1048.02 seconds. Its real ARM64-emulated transport workload exceeded the old
900-second quality-runner subprocess ceiling. The ceiling is now 1800 seconds;
every command, test, security check and coverage threshold remains mandatory.
Fresh clean-room and canonical gate runs certify the final source independently.

The first transport corrective, `1a1c0f8b9194cadbea2fac00816f96d155b52d5e`,
passed local gates and exact-head Security. Its CI integration job was cancelled at
the 90-minute job limit: native fixture compilation took 72 minutes 35 seconds,
leaving insufficient time for the unchanged full protocol/regression suite.
The follow-up raises only that job ceiling to 120 minutes. No test, coverage,
transport, architecture or security gate is removed. The cancelled run remains
auditable as `35541036432`; it is not represented as a successful certification.
Runtime, migrations, tests and native configuration remain identical to that commit.
After the execution lock expired on a clean tree, canonical recovery/reacquisition
continued the same BUILDING phase; no phase restart or closure occurred.

## HISTORICAL — pre-candidate development and continuation

All subsequent checkpoint descriptions, outstanding-work statements, lock times
and intermediate test counts in this historical section describe their own earlier
development moment. They are not current candidate status or missing-work claims.

### Original development status

This is an implementation workspace awaiting complete candidate certification and
independent audit, not a READY/GO declaration. Canonical main remains through P18. The feature branch
is BUILDING/PENDING, started once through `make nxs-start PHASE=NXS-P19 ACTOR=cdxm`
at 2026-09-19T19:31:44Z. The published governance baseline is
`da0f28f6999e199f5348d63985292c8b93aa8f23`. Do not start the phase again.

Historical continuation checkpoints: `24f64003a8fb077e0cfd411ad000e5138757a781`
and `f1befaf604448ce4645f64368f3f1733665bcddf` were created locally for preservation,
not as final implementation candidates. After checkpointing the dirty work and verifying a clean tree,
canonical lock recovery and reacquisition ran on 2026-09-20 at 06:37:50Z; the active
`cdxm` lock expires at 10:37:50Z. Recheck actual lock status before continuing.
The original phase start timestamp is unchanged. No push or closure had occurred
at that historical checkpoint. Consult current Git history and canonical lock/status
tooling for later continuation; closure remains unauthorized.

## Implemented foundations, not complete acceptance

- Bounded request contracts, canonical transaction fingerprints, safe target-IP
  validation and request-signature verification primitives. Signature verification
  is explicitly not replay authorization; PostgreSQL nonce arbitration now precedes
  internal API dispatch, with per-edge serialization and bounded expiry cleanup.
- Minimal DID discovery projection with a separate non-bypass lookup role,
  exact-key restrictive read policy, composite P11 source binding, forced tenant
  RLS for revalidation, and synchronous number/account projection triggers.
  P11 internal number writes acquire the account lock before modifying the number.
- Target/head/mutation-record schema, immutable target tuples, unique active
  target, legal state-transition constraints and platform `sip_edge:control`.
  Registration uses Cell serialization, expected revision and durable receipts.
  Activation/rotation/drainage/retirement now use revision receipts and durable
  outstanding-route references. Complete control/retirement race certification remains.
- Eight additive development migrations, `d19a0b1c2d3e` through `d19b1b1c2d3e`.
  Earlier deployed migrations are unchanged.
- Inbound P11 revalidation and shared P18 admission, durable authorization and
  initial-issue CAS; encrypted internal route handles bind the authenticated edge/boot.
- Durable one-slot-per-call egress permits, immutable upstream revisions, atomic
  consumption/route/history, and the private P11 ARI channel-variable seam.
- Explicit platform-controlled tenant/account keyset backfill, bounded to 100 numbers,
  inserts missing discovery rows without overwriting newer projections.
- Durable immutable dialog binding and signed result callbacks retain route references
  until a successful explicit terminal observation. No elapsed-time owner recovery.

## Historical continuation protocol checks (2026-09-20)

A focused disposable PostgreSQL run executed five integration tests successfully
in 3.09 seconds: inbound issue/retirement, egress consumption, locator backfill,
real Kamailio wire signaling, and real Asterisk ARI/PJSIP propagation. Upgrade to
`d19f0b1c2d3e`, Alembic check, downgrade to P18, re-upgrade and schema guard passed.
The wire fixture exchanged INVITE/200/ACK/BYE/200 and verified durable ESTABLISHED
then ENDED state and release of the target reference. Negative preloaded Route,
unapproved ingress host, REGISTER and exhausted Max-Forwards requests received
rejections without a packet in the target socket. This is not the complete SIP matrix.

The Asterisk fixture uses actual HTTPS ARI and PJSIP, not a fake adapter; its opaque
server-created variable reached exactly one internal header and was absent from logs.
An initial fixture startup failure was caused by a temporary configuration directory
unreadable to UID 10001; making the directory traversable fixed it without granting
root or making the container writable. A later composed test now passes the complete
P11 → permit → HTTPS ARI → Asterisk/PJSIP → Kamailio → carrier-UAS path, proving
the server-only token is consumed and stripped before the carrier leg.

Repository-owned Kamailio 6.1.4 images built from the publisher-checksummed source
for amd64 and arm64; both executed native version inspection as UID 10001. Native
reference configuration validation passed on both architectures. This resolves the earlier
upstream-image architecture availability question, but does not certify complete
arm64 protocol behavior or clean-room reproducibility.

The current Wolfi-based Kamailio and Asterisk images passed pinned Trivy 0.74.0
HIGH/CRITICAL scanning with zero findings. Previous Debian-based images failed
with 89 and 67 findings respectively; no ignore list or weakened gate was used.
The scanner does not independently certify all source-built C code.

Further executed checks include explicit response loss after committed egress
consumption (two parameterized cases passed), real PostgreSQL suspension/admission
lock ordering in both directions, and real wire CANCEL/non-2xx ACK, re-INVITE,
UPDATE and INFO alongside two-edge routing and target rotation. Six native tests
passed, covering both architectures and invalid trust/resource-bound startup.
These results do not constitute a completed C/AC matrix.

The dedicated internal resolver now has explicit secret/configuration loading,
non-bypass database-role startup checks and a P11 issuer seam enabled only through
trusted server settings. Peer policies are durable, revision-fenced and revalidated
inside authorization; account-to-upstream selection is tenant-scoped PostgreSQL state.

A subsequent full-suite attempt recorded 2064 passed / 2 failed, coverage 90.99%.
Both failures came from the temporary disposable-DB harness supplying a SQLAlchemy
URL to tests that call asyncpg directly. Correcting that harness's superuser DSN
made both existing migration regressions pass without changing their assertions.
A new full-suite run is required for current-source certification.

## Historical foundation validation (before continuation changes)

The latest disposable PostgreSQL foundation run executed 83 tests successfully
(3.30 seconds), including 33 Organizations, stale/foreign discovery rejection,
account/number mutation synchronization, deletion/recreation identity fencing,
target SQL constraints and concurrent registration receipt identity.
The same disposable database passed P18-to-P19 upgrade, Alembic drift check,
P19-to-P18 downgrade, re-upgrade and schema guard. See
`.nxs/evidence/NXS-P19/development-checks.json` for the exact source hashes and scope.
These checks do not prove actual SIP sends, complete route admission or the C/AC matrix.

The first development full-suite run produced 2033 passed / 1 failed with 90.79%
coverage. Its failure was the existing OTP scope contract still requiring P19 to
be PLANNED. That assertion now permits the authorized P19 lifecycle while keeping
P20–P32 PLANNED/PENDING. No coverage threshold or exclusion was weakened.

A second development suite produced 2040 passed / 5 failed. Two event-consumer
tests encountered NATS service-unavailable errors while a separate focused run
reset the shared test JetStream streams. Database isolation alone was insufficient
to isolate those runs. The migration round-trip also found an older local copy of
the uncommitted target migration without its later-added head-fencing trigger;
the downgrade failed, followed by duplicate fixture data and an unclosed-resource
warning. These failures are not passing evidence. Subsequent full-suite validation
must use a fresh disposable database with no concurrent test process sharing NATS.

That sequential fresh-database rerun passed: 2045 tests, zero failures or skips,
91.15% coverage, 437.44 seconds. The earlier failures remain recorded in
`.nxs/evidence/NXS-P19/development-regression.json`. This is a regression result,
not evidence that the missing SIP implementation or acceptance tests exist.
The original reusable local development database can still contain the earlier
uncommitted migration definition; use a fresh disposable database for continuation
until that development schema is deliberately rebuilt. Do not alter a production DB.

Lint, typing (323 source files), Bandit and pip-audit passed. pip-audit reports
the local `nexus-ai` package itself as unavailable on PyPI and therefore not audited.
Gitleaks source-snapshot scanning (including untracked implementation files) and
Git-history scanning passed without leaks. No container-scan or clean-room PASS
is claimed.

The inspected official Kamailio 6.1.4-bookworm artifact provides linux/amd64 plus
an attestation, not an arm64 runtime image. Native version inspection succeeded.
This is neither native reference-config validation nor multiarch certification.
The artifact digest and limitations are recorded separately.

## Continuation verification history

Continuation development verification on 2026-09-20 additionally executed the
full disposable-database suite: 2086 passed, zero failed, 91.05% coverage, 431.02
seconds. Later changes require a new final regression; this is historical evidence,
not a final candidate certificate. The earlier two harness failures remain recorded.

Both amd64 and emulated arm64 now execute real Kamailio SIP interaction, not only
native configuration validation. The combined upstream-control/two-architecture
run passed 13 tests in 100.58 seconds. HTTPS certificate validation, dialog topology
masking, bidirectional INFO, reliable provisional response/PRACK, two-Cell routing,
unknown-source relay denial and committed issue-response loss are exercised.
The real P11/ARI/Asterisk/PJSIP/Kamailio path also records terminal egress results;
protocol ACK reception is not assumed to imply the asynchronous DB observation
already committed. Tests synchronize on the actual resolver result transaction.

Upstream-account changes now retain tenant-scoped, append-only DB-generated
revision history. Tenant runtime deletion cannot erase the current binding;
competing rotations have one revision winner and invalidate unconsumed old permits.
No automatic carrier retry or fresh per-call permit is introduced.

The subsequent canonical clean-room executed 2135 tests with zero failures and
91.11% coverage, including fresh PostgreSQL/migrations, both Kamailio architectures,
real SIP and real Asterisk permit propagation. Later wire assertions added explicit
retransmission, established-dialog suspension and graceful container-stop checks.
The first full mapping run on those assertions recorded 2134 passed / 1 failed,
90.82% coverage and no source drift. Its failed amd64 committed-issue-response-loss
case is retained in `execution-criteria-attempt-1.json`; an isolated rerun passed,
which does not erase the full-run failure. Current certification requires a complete
successful rerun, not selective promotion of that isolated result.

Durable peer/upstream control, exact role startup/readiness, protocol result binding,
bounded backfill and the complete C/AC test mapping now exist. The authoritative
candidate outcomes are the current execution-criteria, quality-gates, clean-room
and quality-matrix artifacts, not the historical counts above. Exact candidate-head
CI/Security remains a separate external gate; local checks cannot declare it PASS.

No automatic failover, relocation, media migration, capacity certification or
production deployment is implemented or claimed. Do not close, merge or start P20.
