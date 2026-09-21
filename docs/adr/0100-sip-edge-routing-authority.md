# ADR-0100: SIP edge routing authority and pinned dialogs

Status: externally approved P19 implementation contract. Its historical governance
state was PLANNED/PENDING. P19 is now BUILDING/PENDING on its feature branch after
separate implementation authorization. The runtime candidate and local executable
proofs exist; exact-head validation and independent certification remain separate
gates recorded under `.nxs/evidence/NXS-P19/`. No READY/GO or merge is declared here.
This ADR refines historical ADR-0013; it does not itself implement Kamailio.
Canonical baseline is `e2114cfe8f150e85b9ae432a9af557ceb52cf836`, through P18 READY/GO.

## Decision

Kamailio is the SIP signaling data plane; Asterisk remains the telephony/media
engine controlled through P11 ARI. Organization/organization_id remains the tenant.
P11 owns verified DID/account/call/DTMF/media-session identity; P12 retains its
external voice/media bridge. P18 alone owns Organization-to-Cell placement. P19
owns Cell-to-SIP-target control and new-dialog signaling authorization. Neither
SIP domains, carrier profiles, edge nodes nor Cells become tenant identities.

An authenticated edge submits a bounded canonical DID and SIP transaction identity
to one internal resolver, never Organization/Cell/host authority. The resolver
uses a minimal global SipDidLocator indexed by canonical E.164 to discover one
candidate tenant, then revalidates exact P11 number/account/E.164 and enabled,
verified, active status under forced RLS, then P18 ACTIVE placement and the one
active PostgreSQL P19 target. The locator is NOT DID ownership authority. Final
authorization locks/rechecks its immutable ID/revision and canonical P11 rows.
Missing/stale/disagreeing entries fail closed, never trigger a tenant scan.
It creates a durable, exact-tuple route authorization before returning a route.
No tenant database credential reaches Kamailio. The existing webhook lookup is
not misrepresented as a globally readable DID directory. The engineering contract
specifies locator-specific least privilege, exact source FKs and same-transaction
P11 source/projection maintenance. No BYPASSRLS, SECURITY DEFINER shortcut or broad
tenant SELECT is permitted. Carrier profiles authenticate traffic without listing
every tenant; shared carriers exceeding 32 Organizations use the same indexed
discovery. Internal P11 synchronization seams may change later, but its ownership,
public API, call state and RLS authority do not.

PostgreSQL commit of route authorization is the new-dialog linearization point.
Placement suspension first denies authorization; authorization first may proceed
to its pinned target without physical cancellation guarantees. Resolution alone
is not admission: P18 shared-lock admission and target verification participate
in that same transaction. No database transaction spans SIP or resolver network I/O.

Targets have immutable address tuples and monotonic Cell-wide revisions, with
REGISTERED → ACTIVE → DRAINING → RETIRED and REGISTERED → RETIRED. A per-Cell
serialized current pointer and database uniqueness prevent two active targets.
Rotation atomically drains the old target and activates a registered revision of
the same Cell. Existing authorizations/dialogs stay pinned; retirement cannot
discard outstanding dialog references. Expected revision, durable idempotency
receipts and append-only control history fence stale mutations. Rotation is not
Organization relocation, health-based failover or active-call migration.

Established SIP routing uses validated transaction/dialog state and a safe edge
Record-Route/route set. A new INVITE uses current authority; ACK/CANCEL/BYE and
in-dialog requests use the existing binding, not new placement selection. Header
presence alone is never proof of a dialog. Duplicate transactions preserve the
same durable route/edge owner; ambiguous transmission or owner loss does not
authorize another edge to create a new downstream dialog.

Outbound calls use P11 durable call → P19 SipEgressPermit → ARI server-generated
channel variable → operations-owned PJSIP opaque header → authenticated Asterisk
peer at Kamailio → P19 atomic consumption → bound approved upstream. Both trusted
peer AND permit are mandatory; IP, caller ID, SIP Call-ID or client UUID alone
grant nothing. The implemented internal adapter carries the inherited server-generated
ARI variable through the operations-owned PJSIP header; real Asterisk tests prove
propagation without changing public P11 call/idempotency/account/media semantics.
The original governance obligation is now executable proof, not deferred design.
No tenant or LLM supplies or sees tokens. Pre-ARI permit denial follows P11's durable
FAILED path; ambiguous external I/O retains its original fences.

The pre-ARI corrective makes that boundary durable even through total database
unavailability: call/event/PENDING admission owner commit together. A live permit
and immutable owner CAS must commit DISPATCHED before ARI. Bounded tenant recovery
may revoke only expired PENDING work, atomically fencing the old owner and failing
the call with its P04 event. DISPATCHED work remains conservatively ambiguous until
the provider result is known; it is never automatically re-originated. This is an
internal P11 integration fence, not a public API change or P25 SIP failover.

The tenant-scoped permit binds P11 call/account, Cell/placement generation,
destination fingerprint, upstream ID/revision, Asterisk identity, semantic
fingerprint and DB-time 30-second validity. Minting is conditional; consumption
revalidates authority and atomically grants one initial relay, pins transaction/
edge ownership and persists history. A unique per-call slot survives expiry and
ambiguity. Same-transaction replay returns state, not a second send grant; live
SIP retransmissions retain the original branch. Wrong peer/destination/revision
denies. Lost consumption response never mints another permit or carrier route;
P25 owns automatic reconciliation. Only an opaque secret token travels on the
internal SIP leg and is stripped before external relay. Tenant destinations remain P11-canonical
numbers/aliases, never hostnames or Route headers. No arbitrary Internet relay,
3xx retargeting or automatic alternate-carrier selection is allowed.

## Security and failure policy

Use a dedicated internal service identity distinct from tenant RBAC; target/peer
administration requires explicit platform `sip_edge:control`, following platform
grant precedent, not inherited `cell:control` or Organization owner authority.
Authenticate edge identity before consuming its peer observations. Strip forged
internal headers; reject unknown peers, missing DID/placement/target, stale or
inconsistent tuples and malformed/oversized requests. Enforce method, hop, size,
deadline and abuse limits. Database/resolver failure produces bounded rejection,
never cached authorization, random routing or fan-out. Protect signaling topology;
do not claim SDP/media privacy without a separately governed media solution.

Valkey/NATS/events are not authority. Global target mutations retain durable
control history; P04 tenant outbox semantics must not be falsely applied to global
inventory events. Tenant route events, if emitted, commit with route state using
the existing P04 envelope. Logs/events contain safe IDs/reasons only, no raw SIP,
SDP, credentials or tenant content. P22/P24 broader platforms remain separate.

## Consequences, proof and exclusions

Multiple edges independently resolve new transactions through the same authority;
no process-local singleton supplies placement. Dialog continuity still depends
on its owning edge: lossless node failover is not certified. Require real SIP
traffic through two Kamailio edges/two SIP UAS targets plus independent PostgreSQL
race sessions, native config validation and immutable artifact provenance.

The detailed models, RLS bootstrap, exact locking, replay, failure semantics and
C01–C32 / AC01–AC43 certification contract is in
`../engineering/nxs-p19-sip-edge-scaling-design.md`; results/source bindings reside
under `.nxs/evidence/NXS-P19/`. Supplemental transport tests preserve these IDs.
UDP/TCP/TLS are implemented transports, not authority selectable by SIP headers.
Plain transports require controlled networking; TLS uses native certificate state,
CA validation and exact immutable pins. Dialogs retain transport across rotation.
The runtime guide records the explicit source-build patch enforcing outgoing TLS
pins before queued transaction data is written.
No P11/P12/P18 redesign, relocation, database sharding, RTP relay/transcoding,
media migration, automatic failover, Nomad/fleet orchestration, frontend, capacity,
production SLA or deployment is authorized. P19 is distinct from P20 Sentinel,
P21 compliance, P22 audit, P23 cost, P24 global observability, P25 recovery,
P26 DR, P27 final hardening, P28 capacity, P29 chaos and P32 deployment.
