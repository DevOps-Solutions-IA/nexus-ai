# ADR-0100: SIP edge routing authority and pinned dialogs

Status: proposed P19 implementation contract, governance only. NXS-P19 remains
PLANNED/PENDING. This ADR refines historical ADR-0013; it does not implement Kamailio.
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
uses P11 phone-number ownership within operations-approved account scopes under
forced RLS, then P18 ACTIVE placement, then the one active PostgreSQL P19 target.
It creates a durable, exact-tuple route authorization before returning a route.
No tenant database credential reaches Kamailio. The existing webhook lookup is
not misrepresented as a globally readable DID directory. The engineering contract
specifies bounded account-scoped discovery and authoritative revalidation.

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

Outbound calls still originate through P11 → ARI → Asterisk. Only authenticated
internal Asterisk peers bound to the Cell and P11 account/call may use an
operations-approved upstream profile. Tenant destinations remain P11-canonical
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
future C01–C25 / AC01–AC30 proof obligations are in
`../engineering/nxs-p19-sip-edge-scaling-design.md`. None is marked executed here.
No P11/P12/P18 redesign, relocation, database sharding, RTP relay/transcoding,
media migration, automatic failover, Nomad/fleet orchestration, frontend, capacity,
production SLA or deployment is authorized. P19 is distinct from P20 Sentinel,
P21 compliance, P22 audit, P23 cost, P24 global observability, P25 recovery,
P26 DR, P27 final hardening, P28 capacity, P29 chaos and P32 deployment.
