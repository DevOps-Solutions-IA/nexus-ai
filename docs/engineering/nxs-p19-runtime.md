# P19 SIP edge runtime and laboratory boundaries

P19 is BUILDING/PENDING on `feat/nxs-p19-sip-scaling`. This document describes the
implementation candidate, not canonical-main deployment, closure or capacity
certification. Main remains through P18. ADR-0100 and the P19 design remain the
authority contract; `.nxs/` and executed evidence determine certification status.

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

See `infrastructure/kamailio/README.md` for pinned Kamailio 6.1.4, publisher source
checksums, non-root images, bounds and secret mounts. This reference certifies only
UDP SIP on explicitly isolated links; it makes no SIP TCP/TLS or Internet-grade
source-address authentication claim. Unsupported transports fail closed. Resolver
HTTPS and real Asterisk TLS ARI are separate, tested transport boundaries.

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

Only additive P19 migrations are changed. Disposable downgrade/re-upgrade is a test,
not permission to destroy durable permits or ambiguous call references in production.
Stop new signaling first; preserve consumed fences and control history, and prefer
reviewed roll-forward. No cross-Cell relocation, automatic carrier/Cell failover,
edge takeover, SIP registrar, RTPengine, media migration, Nomad deployment or capacity
claim is introduced. P20/P24/P25/P28/P29/P32 remain separate governed phases.

P19 closure, merge, deployment and P20 start are not authorized by this implementation
task. Exact candidate-head CI/Security and independent implementation audit precede
any later closure authorization.
