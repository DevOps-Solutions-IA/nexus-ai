# P19 implementation development status

This is an incomplete, uncommitted development workspace, not an implementation
candidate or certification. Canonical main remains through P18. The feature branch
is BUILDING/PENDING, started once through `make nxs-start PHASE=NXS-P19 ACTOR=cdxm`
at 2026-09-19T19:31:44Z. Base HEAD remains
`da0f28f6999e199f5348d63985292c8b93aa8f23`. Do not start the phase again.
Recheck lock validity before continuing; the recorded expiry is
2026-09-19T23:31:44Z. No implementation commit, push or closure has occurred.

## Implemented foundations, not complete acceptance

- Bounded request contracts, canonical transaction fingerprints, safe target-IP
  validation and request-signature verification primitives. Signature verification
  is explicitly not replay authorization; durable replay arbitration is absent.
- Minimal DID discovery projection with a separate non-bypass lookup role,
  exact-key restrictive read policy, composite P11 source binding, forced tenant
  RLS for revalidation, and synchronous number/account projection triggers.
  P11 internal number writes acquire the account lock before modifying the number.
- Target/head/mutation-record schema, immutable target tuples, unique active
  target, legal state-transition constraints and platform `sip_edge:control`.
  Registration uses Cell serialization, expected revision and durable receipts.
  Registration and bounded listing do not implement the complete target lifecycle.
- Two additive development migrations, `d19a0b1c2d3e` and `d19b0b1c2d3e`.
  Earlier deployed migrations are unchanged.

## Executed scoped validation

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

## Required remaining implementation

All C01–C32 and AC01–AC43 remain uncertified. Partial unit/database coverage must
not be promoted to an end-to-end criterion PASS. Outstanding work includes:

- Bounded audited locator backfill and complete mutation/failure/race matrix.
- Target activation, rotation, drainage and safe retirement tied to outstanding
  durable route references; peer/upstream administration and revisions.
- Authenticated internal API, PostgreSQL replay arbitration, P18 admission
  composition, route authorization/issue CAS and durable dialog ownership.
- Per-call egress permits, encrypted replay material if needed, TTL/consumption
  fences, P11 private adapter integration and real Asterisk/PJSIP propagation.
- Pinned Kamailio reference configuration, native config validation, real SIP
  two-edge/two-Cell tests, method/dialog security and failure injection.
- Complete C/AC evidence mappings, security/container scanning, clean-room,
  supported architecture validation, final full regression and exact-head CI.

No automatic failover, relocation, media migration, capacity certification or
production deployment is implemented or claimed. Do not close, merge or start P20.
