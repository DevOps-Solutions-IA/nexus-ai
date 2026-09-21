# P19 SIP edge runtime and laboratory boundaries

P19 is closed READY/GO on `feat/nxs-p19-sip-scaling`, binding externally audited
implementation `860d9a129adad75d0e4d7f8470c8a9d9f224a5be`. Main remains through P18:
P19 is not merged or production deployed. External closure audit is pending; P20
has not started. No capacity or automatic failover certification is claimed.
ADR-0100 and the P19 design remain the authority contract; `.nxs/` and executed
evidence determine certification status.

## Ownership and persistence

Organization remains the tenant. P11 owns accounts, numbers, calls, ARI, DTMF and
media; P12 owns its voice/media provider path. P18 alone owns placement and its
generation. P19 consumes shared-lock placement admission and owns SIP targets,
new-dialog authorizations, egress permits and signaling dialog bindings.

The locator is an indexed discovery projection, not DID ownership. Its dedicated
`nexus_sip_locator` role has exact-E.164 access and no tenant-table privileges.
Final P11 revalidation runs under forced RLS as `nexus_runtime`; both roles are
non-superuser/non-BYPASSRLS. Source triggers synchronize numbers/accounts and their
locators in the same transaction, without SECURITY DEFINER. Missing, revoked or
stale discovery cannot fall back to scanning tenants. Deleted/recreated sources
have different locator identities. Explicit backfill is tenant/account scoped,
keyset bounded to 100, and cannot replace a newer projection.

Cell targets use immutable address revisions and a single active-target constraint.
REGISTERED → ACTIVE → DRAINING → RETIRED and REGISTERED → RETIRED are the only
transitions. Rotation atomically drains the prior revision. Durable receipts and
history fence duplicate/stale mutations. Outstanding issued, established or
ambiguous routes block retirement; elapsed time cannot prove an external call ended.

Inbound authorization holds P18/P11/peer/target authority through one PostgreSQL
commit. Its DB-time issue deadline is five seconds. Only one issue CAS grants
initial relay. Egress has one permanent permit slot per P11 call and a 30-second
unconsumed TTL; encrypted opaque material contains at least 256 random bits and
only its digest is stored. Authenticated Asterisk identity, exact Cell/generation,
destination and upstream revision are all rechecked during atomic consumption.
Account-upstream rotations are platform controlled and retain append-only revision
history. They cannot redirect an already-consumed route.

Dialog observations bind Call-ID and both tags to the original edge/boot/transaction.
They do not acquire new placement authority. Explicit terminal observations retain
history and release inbound target references. Egress terminal state never deletes
the per-call permit fence. Lost issue/consume/result responses cannot mint a new
permit, route owner or carrier choice. Automatic reconciliation is P25, not P19.

## Trusted configuration and rollout

The ordinary tenant API does not expose SIP route selection. The dedicated internal
ASGI factory is `nexus_ai.sip_edge.runtime:create_application`. It requires explicit
`NXS_SIP_EDGE__ENABLED`, `NXS_SIP_EDGE__SECRET_FILE` and
`NXS_SIP_EDGE__LOCATOR_DSN`, plus the normal runtime database configuration.
Secret material is loaded from a bounded regular file, not tenant headers/options.
Use owner-only or narrowly group-readable mounts; world-readable or writable
secret files fail startup. Per-edge HMAC keys, permit/handle keys and peer policies
are never committed. The factory verifies both DB roles and exposes dependency-aware
`/health/ready` separately from `/health/live`.

HMAC authenticates method/path/body/edge/boot/timestamp/nonce with at most 30 seconds
of skew. PostgreSQL serializes replay receipts; a cache cannot grant authentication.
The reference edge also verifies HTTPS resolver certificates against a mounted CA.
Its shared per-request deadline bounds authorization plus issue, not each socket
read independently. Missing service identity fails before tenant discovery.

Provisioning is explicit: platform `sip_edge:control`, valid P11 account/number,
P18 placement, peer policy, target registration/activation, and account/upstream
binding for outbound. No migration invents trust, target, carrier or placement.
Existing numbers without a valid locator do not route. Existing P11 public call
semantics remain unchanged; enabling the internal SIP issuer makes the server-owned
egress permit mandatory before ARI I/O. DB transactions end before provider I/O.

The ARI adapter supplies inherited `__NXS_SIP_EGRESS_PERMIT` at channel creation.
The operations-owned PJSIP pre-dial handler sends its opaque value in
`X-NXS-Egress-Permit` only on the trusted internal leg. Kamailio strips it before
carrier relay. Request metadata, Caller-ID and source address cannot supply a permit.

## Reference edge and reproduction

P19-governed P11 call creation atomically persists the call, CREATED event and
`sip_call_admissions` PENDING record. Its immutable owner and database-time 30-second
deadline distinguish pre-provider work from an external dispatch. Permit issuance
retains its existing P18/P11/upstream transaction and unique slot per call. Before
ARI, a separate call-lock/owner CAS commits PENDING → DISPATCHED and
`NXS_TELEPHONY_PROVIDER_DISPATCH_UNCONFIRMED`; it requires a live AUTHORIZED permit.
Only that owner may proceed. No PostgreSQL transaction spans ARI or secret retrieval.

Admission denial revokes PENDING and its unconsumed permit, and commits FAILED plus
one P04 failure event. If the entire database is unavailable, the durable PENDING
fence survives; no failure write is claimed during the outage. After recovery,
`TelephonyService.recover_pending_admissions(organization_id, limit=100)` performs a
bounded tenant-scoped sweep. Call reads/lists and same-key replay also reconcile
expired PENDING records. The sweep returns the number of bounded candidates examined,
including already-terminal calls whose admission fence is revoked, not a count of
new FAILED events. Repeated batches reach zero without skipping those terminal rows.
This includes calls without idempotency keys. Operations
must run bounded recovery after a database outage; no global unbounded sweep or
background failover coordinator is introduced.

Recovery locks call → admission → permit and atomically changes PENDING → REVOKED,
revokes any AUTHORIZED permit and writes FAILED/event. Expiry alone is not authority:
the irreversible state CAS fences a resumed old owner before ARI. A concurrent replay
before the deadline cannot revoke the owner. Neither recovery nor retry issues a
replacement permit. A committed permit followed by a pre-dispatch crash is revoked
the same way. DISPATCHED is never reclaimed: a crash after its commit, even before
physical dispatch, is conservatively uncertain and never automatically re-originated.
Successful ARI linkage clears the dispatch marker; post-ARI timeout retains existing
P11 ambiguity semantics. This narrow pre-provider recovery is not SIP/Cell/carrier
failover or P25 reconciliation of potentially issued external work.

Legacy calls without an admission record are not guessed safe to terminate. The
additive migration does not retrospectively assert that historical ARI effects were
absent. Recovery is for calls governed by the new durable fence.

See `infrastructure/kamailio/README.md` for pinned Kamailio 6.1.4, publisher source
checksums, non-root images, bounds and secret mounts. UDP/TCP require explicitly
isolated links; neither claims cryptographic source authentication. SIP TLS verifies
CA chains and exact leaf fingerprints from native connection state. Configured,
authorized and actual transports must agree, without fallback. Typed destinations
use SIP for UDP/TCP and SIPS for TLS, including bracketed IPv6 addresses.
Equality is per signaling leg: an authenticated UDP Asterisk leg may have an
explicitly authorized TLS carrier leg. That is not an implicit transport downgrade.
TLS identities are immutable target/upstream revision attributes. Database CHECKs
require a fingerprint for TLS and forbid one for plaintext. The reference validates
secret permissions/expiry and pins downstream certificates before any SIP bytes.
See the reference README for the explicit 6.1.4 connection-authentication patch
and matching OpenSSL build provenance. Resolver HTTPS and Asterisk TLS ARI remain
separate security boundaries; neither substitutes for SIP TLS evidence.

Native transaction/dialog state, authenticated direction, tags, sequence semantics,
safe route sets and the pinned remote target all constrain in-dialog requests.
Record-Route/To-tag alone never grants authority. Native topoh masks signaling
topology; no SDP/RTP privacy, media anchoring or transcoding is claimed.

`bash scripts/build-sip-fixtures.sh` builds amd64/arm64 Kamailio and amd64 Asterisk
from pinned sources and writes local BuildKit digest metadata under `.nxs/runtime/`.
`make clean-room` provisions fresh dependencies/PostgreSQL, builds those fixtures,
executes the full suite including real SIP and ARI propagation, and validates the
application container/multiarch lifecycle. ARM64 protocol testing uses explicit
host emulation, not a throughput benchmark. QEMU installation is build-host setup;
the SIP/Asterisk runtime fixtures themselves are non-privileged, read-only, have
all capabilities dropped and never mount the Docker socket or host PID namespace.
The pre-existing application clean-room smoke uses host networking only to reach
its disposable published dependencies; this is not a production network design.

Run tests sequentially against the shared NATS fixture: separate PostgreSQL databases
do not isolate JetStream reset operations. `python -m scripts.nxs_p19_evidence`
executes the full suite and maps C01–C32/AC01–AC43 to actual nodes and source hashes.
It preserves the separate exact-head CI/clean-room obligation rather than inventing
GitHub PASS from local tests. Coverage remains at least 90%, without P19 exclusions.

## Rollback and exclusions

Only additive P19 migrations are changed. The TLS-identity migration refuses legacy
TLS rows without pins rather than inventing trust; inventory such rows before
upgrade and resolve them under explicit platform migration authority. Published UDP
rows are unchanged. TLS pin removal is unsafe with live TLS authorizations.
Disposable downgrade/re-upgrade is a test,
not permission to destroy durable permits or ambiguous call references in production.
Stop new signaling first; preserve consumed fences and control history, and prefer
reviewed roll-forward. No cross-Cell relocation, automatic carrier/Cell failover,
edge takeover, SIP registrar, RTPengine, media migration, Nomad deployment or capacity
claim is introduced. P20/P24/P25/P28/P29/P32 remain separate governed phases.

P19 closure, merge, deployment and P20 start are not authorized by this implementation
task. Exact candidate-head CI/Security and independent implementation audit precede
any later closure authorization.
