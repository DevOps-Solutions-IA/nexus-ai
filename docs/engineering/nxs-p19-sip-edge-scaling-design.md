# NXS-P19 — SIP Edge Scaling implementation contract

## 1. Status, baseline and canonical inputs

Historical governance baseline: PLANNED/PENDING, no implementation, migration, Kamailio config,
execution lock, implementation evidence or certification. All acceptance/race
criteria below are REQUIRED / NOT EXECUTED. A separate implementation authorization
and canonical lifecycle start are required. Branch: `feat/nxs-p19-sip-scaling`.

Current feature-branch state: implementation was separately authorized and started
on 2026-09-19 through `nxs-start`, actor `cdxm`. P19 is BUILDING/PENDING;
the runtime candidate now has executed local C01–C32 and AC01–AC43 test mappings.
The governance obligations below remain binding. Current outcomes and historical
failures are recorded under `.nxs/evidence/NXS-P19/`; exact-head CI/Security and
independent implementation audit remain distinct from local proof and phase closure.

Canonical main `e2114cfe8f150e85b9ae432a9af557ceb52cf836` includes P18 READY/GO.
Exact-main NXS CI 35403869973 and Security 35403870005 were independently verified
completed/success. Dependencies are P11 and P18; NXS-SCALE-002 retains dependencies
NXS-TEL-001 and NXS-SCALE-001. P12 is a preserved integration contract, not a new
hard prerequisite or scope expansion. P20+ remain unactivated.

Sources: ADR-0012/0013 (historical reservations), ADR-0084/0085/0086 (P11),
ADR-0087/0088/0089 (P12), ADR-0099 and P18 design/runtime, ADR-0100 (this decision).
Actual seams include `telephony.service.TelephonyService`,
`telephony.webhooks.InboundTelephonyService`, `telephony.destinations`,
`telephony.providers.asterisk.AsteriskAdapter`, `domain.telephony.repository`,
`voice.bridge`, `voice.service`, `voice.runtime`, `cells.admission` and
`cells.service`. These remain authoritative; P19 does not copy their state machines.

The manifest uses schema 1.0.0, the existing pre-start declarative-manifest path
supported by `_ensure_manifest` and `validate_invariants`, and all 31 canonical
`_DEFAULT_GATES`. No standalone scaffold command exists; do not invoke start to
generate governance. `requirements_implemented` is a mapping, not an implementation
claim when PLANNED. Registry/project state/lock/readiness need no change.

## 2. Objective, terminology and authority

```text
Carrier/PSTN → Kamailio edge → authenticated NXS resolver
    → P11 verified DID/account → Organization
    → P18 ACTIVE placement → Cell + assignment_generation
    → P19 active SIP target + target_revision → Asterisk → P11 → P12
```

| Concept | Authority and exclusion |
| --- | --- |
| Organization / organization_id | Sole tenant identity; forced RLS and composite FKs remain mandatory. |
| P11 | Accounts, globally unique E.164 numbers, calls, destination canonicalization, ARI, DTMF, call state/idempotency and media sessions. |
| P12 | VoiceSession, externalMedia/media-gateway plan and external provider abstraction; no Kamailio media termination. |
| P18 | `organization_placements` (actual table name), Cell catalog, ACTIVE/SUSPENDED state and assignment_generation; not modified by P19. |
| P19 | SIP signaling ingress/egress policy, Cell-to-SIP-target registry, route authorization and pinned signaling target. |
| PostgreSQL | Durable control, revisions, receipts and route authorization. |
| Kamailio | SIP transaction/dialog data plane, not tenant resolver or database authority. |
| Asterisk | Telephony/media engine, not replaced by a proxy or new P19 call engine. |
| Cache/NATS/JetStream | Optional hints/observations only; never grant new routing authority. |

A route snapshot is not a reusable permit. A committed authorization applies to
one logical SIP transaction/dialog only. It does not grant P11 call control,
P12 voice ownership, P13 execution or P17 human ownership. These identities and
existing P09–P18 idempotency/fencing rules survive routing unchanged.

## 3. Package and API boundaries (design, no code)

Use `nexus_ai.sip_edge` for strict contracts, typed errors, service identity,
target registry and route service; `nexus_ai.domain.sip_edge` for persistence.
Reference config eventually belongs under `infrastructure/kamailio/`, never as
tenant-supplied snippets. No runtime directories/configuration are created here.

Proposed bounded surfaces: platform target register/list/activate/drain/retire;
internal inbound/outbound authorize, issue and exact-result notification. No
ordinary tenant-facing route-selection API. Internal route service authentication
is separate from user RBAC. Platform mutations require an active user with explicit
`sip_edge:control` using existing platform-grant conventions; no implicit grant
to Organization admins, `telephony:*`, `voice:*` or `cell:control` holders.

Contracts use `extra="forbid"`, strict UUIDs/enums, canonical fingerprints and
safe RFC9457 NXS errors. Route requests contain no caller-selectable Organization,
Cell, host, port, transport, arbitrary headers or SQL. Lists use UUID keyset cursors,
limit 1–100; no fleet sweep or full-table materialization.

## 4. Durable model and constraints

Names below are conceptual until additive migration design; invariants are binding.

| Record | Classification and minimum fields |
| --- | --- |
| CellSipTarget | Platform-global: UUID, cell_id FK to P18 Cell, Cell-wide target_revision, immutable host/port/transport tuple, state, DB timestamps. |
| CellSipTargetHead | One row per Cell, current active target/revision or none, monotonic control revision for every effective mutation; FK prevents a pointer to another Cell's target. |
| TargetMutationReceipt/History | Platform-global: actor, operation, expected/result revision, target IDs, bounded reason/correlation, key hash/fingerprint, original result, DB timestamp; append-only. |
| PeerProfile / AccountScope | Operations-controlled source authentication and traffic policy. Optional account restrictions are checked after discovery, never enumerated to discover tenants. Tenant-owned account links retain forced RLS and composite FKs. |
| SipDidLocator | Minimal platform-global discovery index: immutable locator_id, canonical unique e164, organization_id, P11 phone_number_id/account_id, positive revision, active and DB timestamps. Exact composite source binding; no ownership, Cell, target, SIP payload or credential authority. |
| SipEgressPermit | Tenant-owned: permit_id, Organization/P11 call/account, Cell/placement_generation, destination fingerprint, approved upstream ID/revision, owning Asterisk identity, semantic fingerprint, DB issued_at/expires_at, token digest, consumption state and exact transaction/edge owner. One durable permit slot per logical P11 call; no raw token in logs/events. |
| UpstreamProfile | Operations-controlled immutable revision of approved trunk address/transport/secret reference; never raw SIP URI or tenant address. Referenced from approved P11 account/endpoint binding. |
| SipRouteAuthorization | Tenant-owned: UUID route_id, P11 number/account (and outbound call) references, direction, Cell/placement identity/generation, immutable target or upstream revision, transaction fingerprint, owning edge/boot identity, authorization/issue/expiry times and terminal/ambiguous status. |
| RouteTransitionHistory | Tenant-owned append-only exact route/state/reason and authenticated actor correlation, no message/SDP payload. |

All tenant rows have organization_id, FORCE RLS with USING/WITH CHECK, tenant-aware
composite FKs, and non-bypass `nexus_runtime`; `nexus_migration` alone migrates.
Global records have platform-control policies, not a tenant read/write exemption.
No SQL grants on tenant tables to Kamailio; no new BYPASSRLS application role.
Unique `(cell_id, target_revision)`, one active-target partial unique index, and a
per-Cell head prevent split active authority; positive int64 revisions never wrap.
Uniqueness and durable receipts arbitrate races, not in-memory locks.

Target host is a canonical private unicast IP literal from an operations-approved
network (IPv4/IPv6); P19 initially rejects DNS names to avoid hidden DNS rotation
or rebinding. Reject loopback, link-local, unspecified, multicast, metadata ranges,
public addresses and URI syntax. Store at most 45 characters, port 1024–65535 on
an explicit allowed SIP-port list, and enum UDP/TCP/TLS. UDP/TCP are permitted only
on explicitly trusted isolated links; TLS verifies the configured peer identity
and never downgrades. There is no userinfo, path, query, Route/Contact, dialplan or
shell field. Public carrier endpoints are a separate approved upstream profile,
not a relaxation of the internal Cell target policy. Hostnames for upstreams must
resolve only to their pinned approved address set; no DNS failover/SRV lottery.

## 5. Target lifecycle, mutation and drainage

Legal transitions: REGISTERED → ACTIVE; REGISTERED → RETIRED; ACTIVE → DRAINING;
DRAINING → RETIRED. RETIRED absorbs; never reactivate an old revision. A new
activation requires a registered immutable version with a new Cell-wide revision.
An activation with an existing ACTIVE target atomically makes that target DRAINING
and installs the new one. Manual drain leaves no active target until activation.
No address tuple is edited in place, including on a suspended Organization.

Serialize on P18 Cell then P19 target-head authority. Every effective mutation
increments control revision; require expected revision. A durable unique
`(operation, caller_key_hash)` receipt and SHA-256 semantic fingerprint include
Cell, target tuple, expected revision and reason. Same key/same input reconstructs
the original result; changed input conflicts, no second mutation/history/event.
Control revision and target revision are not P18 assignment_generation.

DRAINING blocks new authorizations to that target, not existing pinned dialogs.
Retirement requires no outstanding unexpired authorizations or issued/established/
ambiguous routes. An explicit trustworthy dialog end releases its reference;
missing callbacks, edge death or elapsed wall time alone do not prove termination.
Unissued authorizations can expire under DB time; ambiguous issued work remains
fenced for P25, and may deliberately block retirement. No automated stale-owner
reaping, health-based target selection or failover is introduced.

## 6. DID discovery without weakening RLS

P11 `telephony_phone_numbers.e164` is globally unique, but its repository is tenant
scoped. Existing inbound webhooks resolve a signed/token-bound P11 account before
`by_e164`; there is no certified global unscoped DID resolver to pretend to reuse.

P19 uses SipDidLocator solely to discover the tenant context for P11 revalidation.
One indexed equality lookup by canonical E.164 returns at most one candidate
Organization/number/account plus locator_id/revision. It never returns a route.
This constant-count lookup supports a shared carrier serving more than 32
Organizations without a tenant/account list or linear tenant enumeration; index
cost may grow logarithmically, and no throughput claim is made. Carrier profiles
authenticate traffic, not DID ownership. Optional carrier/account restrictions are
checked against the discovered account, not used as a discovery search space.

Open trusted tenant context from the candidate only inside the authenticated
resolver. Under forced RLS, re-read canonical P11 rows and require exact number ID,
account ID, Organization, E.164, verified=true, inbound_enabled=true, account ACTIVE
and Organization ACTIVE. In final authorization lock these rows and the locator;
require the same active locator_id/revision and exact binding. Only then admit P18
placement and authorize the target using the common lock order (pre-lock snapshots
are hints). Missing, revoked, stale or disagreeing locator/P11 state fails closed,
with zero sends and no fallback scan. A locator row alone never authorizes a call.

P11 remains the only ownership authority. Future internal P11 persistence seams
must synchronously maintain the projection in the SAME PostgreSQL transaction as
number registration/ownership creation, verification/inbound changes, account or
number disable/delete. Source mutation + derived locator change + relevant history
commit or roll back together. Account-wide changes use indexed affected-account
rows in deterministic order, never a fleet tenant scan; lock/statement deadlines
roll back the entire operation, never partially commit projection changes. Final
P11 revalidation remains mandatory even with this transactional synchronization.
Organization ACTIVE is independently checked under its authority lock. No async
event consumer can grant locator validity. P11 public APIs, ownership, call state,
idempotency and forced RLS do not change; no independent locator ownership API.

Require unique canonical E.164 and exact composite FK binding to the P11
Organization/number/account/E.164 tuple. Additive source uniqueness/FK support may
be needed; never rewrite a canonical migration. Revocation increments revision;
deletion removes the locator atomically before source deletion. Re-creation uses
a new immutable locator_id, preventing delete/recreate ABA even if revision starts
again. No locator may outlive a deleted source as an active discoverable row.

The locator is sensitive global discovery data, not a public directory. Grant a
dedicated authenticated resolver principal only exact-key lookup of its minimal
columns, with no tenant-table privileges or generic listing API. Use a narrowly
scoped non-superuser/non-BYPASSRLS DB role for this lookup, separate from tenant
sessions. The final transaction reads/locks only its candidate locator through a
tenant-scoped locator policy; the global discovery connection is never held across
that transaction and grants no authority. Final P11 reads still use nexus_runtime
and forced tenant RLS. Projection
DML belongs only to the controlled P11 write seam, constrained to the trusted
tenant and exact source binding through grants/policies; ordinary tenant callers
cannot create arbitrary locator entries. No SECURITY DEFINER function, broad tenant
SELECT, role escalation or Kamailio SQL access. Role/policy and adversarial tests
must prove these separations. Neither locator credentials nor rows reach Kamailio.

## 7. Inbound authorization and exact route contract

1. Edge authenticates the transport peer against an operations-controlled profile;
   validates bounded SIP syntax and method; rejects unsolicited preloaded routes;
   strips internal-looking X-NXS/Organization/Cell/generation headers, case-insensitively.
2. Extract the called number only from the canonical numeric USER part of the
   Request-URI, and only when its HOST matches the authenticated carrier/profile's
   approved ingress domain/host policy.
   Normalize using P11 E.164 vocabulary. To/From/Contact are not fallback DID
   authority. Reject inconsistent/ambiguous called-number representations and URI
   parameters/escapes that can smuggle a new destination. Raw SIP never enters the
   Python resolver; the edge submits a closed normalized request.
3. Authenticate edge identity independently at the resolver. Bind observed peer
   to that edge's allowed profile; a body/header edge_id is not authentication.
   Find the P11-owned number as above and resolve P18 placement via its shared seam.
4. In one tenant transaction, use the lock order below to revalidate all exact
   identities, ACTIVE placement, registered Cell, active target and peer policy.
   Persist the route authorization, owner, stable transaction identity, immutable
   tuple and history/event intent. Commit. This is the authorization linearization
   point, not the later physical INVITE send.
5. Return a complete internal result: contract version, route_id, organization_id,
   cell_id, placement_generation, target_id/revision, target_host/port/transport,
   owning edge instance, authorized_at and issue deadline. No credentials, SIP
   headers, SDP or provider tokens. Edge never forwards these tenant IDs externally.
6. The owning edge marks ISSUED by a PostgreSQL CAS before external send, using
   the exact transaction/owner tuple and unexpired authorization. Commit, then
   `tm` relays to the pinned target. No provider or resolver HTTP I/O inside DB locks.
   The receiver cannot mutate the tuple or use it for another INVITE.

Use a maximum five-second issue window (DB UTC authority). AUTHORIZED → ISSUED →
ESTABLISHED → ENDED; definitive rejection may become FAILED; uncertain issue/result
becomes AMBIGUOUS. AUTHORIZED may become EXPIRED if never issued. Terminal results
absorb and replay without a new send grant. Record protocol failure separately from
P11 call state; P19 does not assert ANSWERED, media ownership or billable delivery.

Authorization committed before suspension/rotation may issue to its old pinned
target within its issue window. Suspension/rotation committed first blocks the old
route for new transactions. Issue CAS consumes an existing logical authorization,
not a new placement grant; it must not refresh generations or deadlines on replay.
This explicitly compares commit order to authorization, not to wire transmission.

Only the first successful AUTHORIZED → ISSUED CAS grants an initial relay. Replaying
ISSUED returns the durable state, not another initial-send grant. The still-live
owning Kamailio transaction may perform SIP retransmissions on its same branch;
if that transaction is absent, even the same edge must not reconstruct an initial
send from the receipt. A lost issue response therefore sacrifices availability,
retains ambiguity and never silently duplicates a dialog.

The central resolver is not a Cell worker and must not construct trusted worker
identity from a client-selected cell_id. Use P18's canonical shared-lock admission
logic with the authenticated resolver's server-derived selected Cell context;
any reusable adaptation must preserve existing worker configuration checks. The
implementation must prove this seam rather than add independent placement SQL
rules or enable P18's pre-placement bootstrap exception for SIP. P19 always requires
an explicit ACTIVE placement.

## 8. Serialization, retries and two-edge consistency

Common lock order: P18 Cell catalog (ascending IDs) → Organization/placement
authority → P11 account/number/call rows (fixed class order, ascending IDs) → locator rows (canonical E.164
order) → P19 peer/profile/head/target
rows in a fixed documented order → permit → route/receipt/history/outbox. Reference discovery
before locks is not authoritative; recheck after locks. Target control uses the same
Cell-first order; it never acquires an earlier class after a target/route lock.
P11 projection maintenance takes source rows before locator rows and never later
acquires placement/Cell locks. Tenant context is never switched while locks are held.
Profile mutation must respect this order and never take tenant locks after profile
locks. Missing per-Cell head creation serializes through the Cell row.

P18 shared admission locks stay held through route authorization commit. Suspension
takes conflicting exclusive authority locks. Head/target shared locks prevent mixed
address/revision reads; activation takes exclusive locks. DID/account revocation
locks serialize against authorization. No long-running DB transaction for SIP I/O.

Logical request identity is a bounded canonical digest of authenticated peer scope,
direction, SIP Call-ID, From-tag, CSeq number/method and top Via branch/sent-by under
the certified SIP parser. Keep transaction and later dialog identity distinct;
dialog binding includes both tags when available. Call-ID alone is insufficient.
Canonical request fingerprint also binds normalized destination/account semantics;
same identity with a changed semantic fingerprint conflicts. Store bounded hashes,
not raw sensitive SIP headers. A different authenticated carrier cannot replay it.

Two edges handling independent transactions see the same tuple for the same valid
state. Duplicate delivery of one transaction returns the same durable route_id and
original edge/boot owner; only that owner can issue it. A different edge must not
relay it again: return bounded non-relay failure or refer internally to the still-live
owner through a validated bounded path, never pick another target. For this phase,
the reference configuration chooses non-relay failure on owner mismatch. On the
owner, Kamailio transaction matching retransmits responses/the same branch, not a
second downstream dialog. Restart changes boot identity and does not recover old
issue rights. Lost response after commit replays the same row; ambiguous issuance
cannot mint a new route/key. This is logical idempotency, not physical exactly-once.

## 9. Established-dialog semantics

Pin the target at initial authorization (before any send), retain it when dialog
establishes. Record-Route keeps subsequent requests on the trusted edge path.
Validate route-set/dialog token, peer direction, Call-ID/tags and allowed remote
target before handling in-dialog requests. `loose_route()` or a To-tag alone is not
authorization; forged/preloaded Route/Record-Route must never select an internal host.

Non-2xx ACK and CANCEL follow the original INVITE transaction; 2xx ACK and BYE follow
the established dialog. Re-INVITE/UPDATE/PRACK/INFO require the supported dialog state
and peer policy; method-specific CSeq rules must follow SIP, not a blanket increment
that breaks ACK/CANCEL. Remote Contact/target refresh cannot redirect outside the
authenticated peer profile or replace the pinned Asterisk target. No serial/parallel
forking, automatic DNS target switch or automatic 3xx redirect follows a failure.

Do not re-resolve P18 on BYE/re-INVITE to move a call. A suspended Organization's
already-authorized dialog may continue or terminate on its original target. P11
hangup remains separate call control. Unknown dialog/transaction fails closed
(481 where SIP requires a response; ACK must not receive an invented response).
Edge loss may interrupt existing dialogs; P19 does not certify recovery or lossless
multi-edge dialog takeover. Local SIP transaction state is protocol machinery,
not placement authority or a horizontally required singleton.

## 10. Outbound boundary

P11 `CreateCallRequest` → TelephonyService durable call/idempotency → governed ARI
adapter → assigned Asterisk → trusted internal SIP peer → Kamailio → approved trunk.
Do not change P11 request/account/call models, signed webhooks, ARI endpoint aliases,
DTMF or media state. P19 adds routing checks around that boundary, not call creation.

### 10.1 Exact permit chain and internal ARI seam

After P11 commits its durable logical call, its trusted internal originator requests
one SipEgressPermit before ARI I/O. P19 checks that exact tenant/call/account and
canonical destination, P18 ACTIVE Cell/generation, approved account-to-upstream
revision and configured owning Asterisk identity. It persists the complete binding
from section 4, a stable semantic fingerprint and an opaque unpredictable token
(at least 256 bits). A unique `(organization_id, call_id)` slot prevents retries
from minting a second permit. Same semantic request returns the original result;
changed destination/upstream/generation conflicts rather than replacing the permit.
The token is a secret: digest lookup, encrypted replay material restricted to the
trusted originator, never plaintext in history/logs/events/prompts or public APIs.

The current Asterisk adapter originates with endpoint/app/callerId and an empty
JSON body; it does NOT already propagate this proof. The future internal adapter
seam must set server-generated ARI body `variables.NXS_SIP_EGRESS_PERMIT` from the
trusted permit result, never request options or tenant headers. ARI supports
creation-time channel variables in its JSON body ([official channels API](https://docs.asterisk.org/Latest_API/API_Documentation/Asterisk_REST_Interface/Channels_REST_API/)).
Operations-owned Asterisk/PJSIP integration copies only this opaque value into a
single internal `X-NXS-Egress-Permit` header on the actual outbound channel before
its initial INVITE. Channel inheritance/pre-dial handling must be tested on the
pinned Asterisk integration; merely setting a variable without proving its wire
delivery is insufficient. Public CreateCallRequest, caller-ID/account authority,
call state, idempotency, DTMF and media semantics remain unchanged.

Kamailio accepts that header ONLY on an authenticated internal Asterisk peer path
and submits token + bounded transaction identity + observed canonical destination
to the authenticated P19 consume endpoint. Both authenticated Asterisk identity
AND valid permit are required. The resolver checks exact owning peer/Cell, existing
P11 call/account, destination fingerprint, placement generation and approved
upstream ID/revision. Source IP, Caller-ID, From, arbitrary Call-ID, client call UUID
and R-URI host are never substitutes. A peer authenticating for another Cell fails.
Strip the token before carrier relay; inbound/untrusted copies are stripped/rejected.
The complete permit tuple is never transported in SIP. The upstream comes solely
from the bound operations-approved profile, never tenant/LLM input or SIP hosts.

### 10.2 Consumption, expiry and ambiguity

Permit states: AUTHORIZED → CONSUMED → ENDED or AMBIGUOUS; AUTHORIZED → EXPIRED
or REVOKED. Terminal/ambiguous records retain the unique call slot and exact binding.
Use PostgreSQL UTC time, a fixed 30-second unconsumed TTL from issued_at, and no
extension on retry. An expired unconsumed permit grants nothing. Expiry of a
consumed permit never proves absence of external work or permits reissuance.

Minting reserves a conditional permit, NOT an unconditional wire authorization.
Consumption, in one transaction using section 8 ordering, rechecks current P11
call/account eligibility, P18 ACTIVE generation, peer policy and exact ACTIVE
upstream revision, then locks the permit. The first valid CAS atomically marks
CONSUMED and creates the exact ISSUED SipRouteAuthorization, transaction/edge/boot
owner, receipt and history/outbox. This commit is outbound initial-relay
authorization; unlike inbound, no separate issue CAS is needed. Suspension or
upstream revision change committing first denies consumption; consumption first
pins the approved tuple. No DB locks span ARI, resolver HTTP or SIP transmission.

Exactly one winning response carries an initial-relay grant. Same transaction/key
replay returns durable state, not another initial-send grant. Another independent
transaction, destination, upstream or owner cannot consume the same permit again.
The still-live owning Kamailio transaction may retransmit its existing SIP branch;
this is not a new dialog grant. Lost consume response after commit retains the
CONSUMED fence (AMBIGUOUS when observed), with no second permit, fresh route,
carrier selection or reconstructed initial send. Edge death and ambiguous ARI
originate similarly retain durable identities. P25 alone owns later automatic
reconciliation. An established dialog stays pinned if profiles change later.

No tenant-controlled upstream/credential/transport selection, unrestricted proxying,
carrier hunting or failover. Existing P11 ambiguous ARI outcomes retain the original
call key; SIP retries do not create another P11 call. No raw provider SDK or media
network path is added by the route resolver.

## 11. Security and resource bounds

- Internal resolver: authenticated HTTPS service request, credentials injected via
  secret references; a distinct edge identity/boot instance and permitted peers.
  Application authentication must reject missing/invalid identity even in tests.
  A bounded signed request (HMAC over method/path/body digest, timestamp and nonce,
  constant-time verification, per-edge secret) is the minimum application seam;
  maximum 30-second skew, durable bounded replay receipts; identical retries reuse
  original semantics, altered bodies fail. Production mTLS/network policy is a
  deployment requirement, not an excuse for an unauthenticated app endpoint.
- Carrier/internal profiles: source CIDRs AND required TLS/trunk identity as configured;
  deny by default. No tenant mutation, wildcard CIDR (`0.0.0.0/0`, `::/0`), wildcard
  trust certificate, arbitrary hostname or public source selecting an internal path.
  CIDR-only UDP trust is an isolated-network assumption, not Internet authentication;
  production must validate anti-spoof controls or require cryptographic peer auth.
- Methods: new INVITE, authenticated health OPTIONS; ACK/CANCEL/BYE and supported
  re-INVITE/UPDATE/PRACK/INFO only in their matched transaction/dialog contexts.
  REGISTER, REFER, MESSAGE, SUBSCRIBE, NOTIFY and unknown methods are denied unless
  separately governed. No registrar or blind transfer introduced.
- Bounds: SIP message 64 KiB, header count 100, individual header 2 KiB, Call-ID/tag/
  branch components 256 bytes, internal JSON 8 KiB, idempotency key 128 characters,
  reason code 64 characters, URI parsing bounded before normalization. Reject duplicate
  conflicting Content-Length, invalid folding, NUL/CRLF injection and encoded URI
  smuggling. UDP datagrams obey the stricter transport limit; no silent truncation.
- Max-Forwards: require valid bounded integer, decrement each proxy hop, stop at zero;
  configured maximum 70. Reject routing loops/preloaded external route sets. Never
  relay to the edge's own listening tuple as a Cell/upstream target.
- Resolver total timeout at most 2 seconds; maximum one same-key retry only before
  definitive issuance, within the original budget. No alternate Cell or retry storm.
  SIP timers bounded by the pinned Kamailio configuration and certified protocol tests.
- Edge resource caps: finite per-peer connection, message-rate, dialog and pending-
  resolver limits in platform configuration; absent/invalid limits fail startup.
  Per-edge limits are defensive only, not a global billing/quota/capacity guarantee.
- Signaling topology: advertise edge-controlled addresses, validate safe Record-Route,
  hide internal Via/Contact/Route address details using tested topology handling; never
  trust user-supplied hidden-route tokens. Reject unauthenticated remote-target refresh.
  No raw SIP/Authorization/SDP logging. SDP address privacy/media anchoring is NOT
  claimed: P11/P12 and deployment own the media path; no RTPengine added silently.
- SQL/SSRF: prepared repository operations only; no dynamic routing expressions,
  user URLs, DNS fallback, shell/dialplan fragments or raw SQL. Logs expose only
  authorized opaque IDs and bounded reasons, not stack traces or topology to callers.

## 12. Events and observability

PostgreSQL mutation + receipt/history is atomic. Global targets/peers are platform
objects, as P18 inventory is: do NOT manufacture an Organization for global P04
outbox events. `sip_edge.target.registered/activated/draining/retired` are reserved
names only; this contract chooses durable global control history and defers global
publication until an appropriate global-event contract exists. No new event bus.

Tenant route authorization/result events, if introduced, use registered P04 versioned
envelopes and the existing tenant transactional outbox, atomic with route state.
Payloads: opaque route/Cell IDs, generation/revision, direction/status/reason and
bounded counters; no DID, credential, raw SIP, SDP, Authorization or secret material.
Duplicate/out-of-order events cannot modify placement, targets or issue rights.
Minimum operational metrics: route_id, authorized edge_id, result/reason, latency,
placement_generation and target_revision. Protect topology-bearing metrics under
platform permissions. No P22 full audit or P24 global SLO platform.

## 13. Failure policy

Wire errors are bounded and disclose no tenant/target details. Internal typed reasons
map to peer-safe SIP outcomes; no error initiates fallback or fan-out.

| Failure | Required outcome |
| --- | --- |
| Unknown/unverified/disabled DID or wrong carrier-account binding | Deny (uniform 404 to authenticated carrier); zero target sends. |
| Missing/revoked/stale locator or source-binding disagreement | Same safe denial; no tenant enumeration or alternate candidate. |
| Missing/expired/altered permit, wrong peer/Cell/destination/upstream revision | Deny; zero upstream INVITEs. |
| Permit consumption response lost after commit | Preserve consumed/ambiguous fence; replay state without a new initial-relay grant. |
| Missing/suspended placement; missing/retired target | Unavailable (503); zero new dialog, no bootstrap default. |
| Resolver/DB down, deadline exceeded | Bounded 503; no cached new authorization. |
| Malformed SIP/URI/headers | 400; oversized input 513; ACK failures are dropped rather than answered. |
| Untrusted ingress/egress identity | 403 or safe pre-auth drop; never relay. |
| Unsupported method | 405; no implicit registration/transfer service. |
| Exhausted hops/loop | 483/482 respectively; stop routing. |
| Stale revision/control mutation | Conflict; zero rows changed, no alternate target. |
| Target rotation during resolution | One committed old/new tuple only, according to locks. |
| Retransmission/lost resolver response | Replay same route/transaction/owner, no new dialog identity. |
| Target rotated/suspended after authorization | Preserve pinned target and existing authorization bounds. |
| Kamailio restart/owner loss | Refuse takeover/reissue; retain ambiguous evidence for P25. |
| Asterisk unavailable | Normal bounded SIP failure; no alternate Cell, target or carrier. |
| Outbox/history write fails | Roll back authorization/control mutation; zero route issued. |
| Result write lost after wire send | Preserve issued/ambiguous fence; no fresh authorization or automatic replay. |

## 14. Required concurrency/protocol matrix

At historical governance authoring, all rows were REQUIRED / NOT EXECUTED. They
remain the binding contract; current executed results and exact test nodes are in
`.nxs/evidence/NXS-P19/execution-criteria.json`. Tests assert route IDs, tuples,
DB rows and actual received INVITEs/dialogs, not only resolver return values.

| ID | Case and required result |
| --- | --- |
| C01 | Two edges resolve same DID concurrently for independent transactions: same authoritative Organization/Cell/target tuple under unchanged state. |
| C02 | P18 suspension vs new INVITE: authorization-first pinned route may proceed; suspension-first zero new authorization/send. |
| C03 | Reactivation: new dialogs use new assignment_generation on same Cell; old generation cannot authorize. |
| C04 | Unknown DID: fail closed, zero Asterisk routes. |
| C05 | Valid Cell without active target: zero fallback/random routes. |
| C06 | Concurrent target activations: at most one active target, stale expected revision loses. |
| C07 | Target rotation vs resolver: atomic old/new tuple, never mixed revision/address. |
| C08 | Forged Organization/Cell/internal headers: strip/reject, no authority. |
| C09 | Untrusted outbound source: denied, no open proxy. |
| C10 | Trusted Asterisk with unapproved upstream: denied, zero upstream INVITEs. |
| C11 | Resolver/DB unavailable: new dialog fails closed; no cache authority. |
| C12 | Resolver timeout, including committed response loss: bounded failure/same-key replay, no alternate Cell. |
| C13 | INVITE retransmission on same/different edges: one logical downstream dialog; owning transaction retained, other edge cannot issue. |
| C14 | Target rotation after establishment: old dialog pinned; independent new dialog uses current target. |
| C15 | Delayed BYE/re-INVITE/ACK/CANCEL: matched transaction/dialog route, not current placement lookup. |
| C16 | Injected R-URI/To/Contact/Route and conflicting lengths: safe rejection, no header/host injection. |
| C17 | Max-Forwards exhaustion/loop: bounded rejection, no recursive resolver loop. |
| C18 | Wrong tenant attempts DID/profile/target manipulation: forced RLS/control denial, zero route authority. |
| C19 | P18 Cell vs requested/forged target mismatch: reject; no source-header override. |
| C20 | Many independent dialogs across two edges: deterministic same Cell, no singleton coordinator. |
| C21 | P11 number/account disable/delete vs authorization: locked-row commit order; no unverified or dangling authorization. |
| C22 | Same control key same/different fingerprint: one receipt/result or deterministic conflict; history atomic. |
| C23 | DB/outbox rollback or worker death after ISSUED: no partial authorization, no new owner/key on restart. |
| C24 | Retire draining target with outstanding/ambiguous dialog: refuse; explicit completed references permit retirement. |
| C25 | Cross-tenant FK/raw SQL, unauthenticated resolver, stale signed request, duplicate/reordered events: deny unauthorized authority. |
| C26 | Shared carrier with DIDs across more than 32 Organizations/accounts: one indexed locator lookup plus one candidate's RLS revalidation; no profile enumeration. |
| C27 | Locator wrong/stale Organization, number, account, E.164, revision or deleted/revoked binding: fail closed, zero target sends. |
| C28 | P11 register/verify/inbound/disable/delete and account mutation race authorization: source/projection atomic rollback and final locks linearize; stale ownership cannot authorize. |
| C29 | Two independent outbound transactions consume one permit concurrently: exactly one initial-relay grant; same-transaction replay grants no second logical dialog. |
| C30 | Valid permit presented by wrong authenticated Asterisk Cell/peer: deny, zero carrier INVITEs. |
| C31 | Trusted Asterisk presents missing/expired/altered permit or mismatched destination/upstream revision: deny, zero carrier INVITEs. |
| C32 | Consume commit succeeds but response is lost: same durable permit/route/owner; no second authorization or alternate carrier. |

## 15. Acceptance matrix

At historical governance authoring, all rows were REQUIRED / NOT EXECUTED.
Current implementation evidence maps these IDs to executed test nodes, artifact
digests, observed counts and failure injections. AC26 also requires separate exact-head
CI/Security; this contract text does not itself declare any criterion PASS.

| ID | Acceptance and required proof |
| --- | --- |
| AC01 | NXS-SCALE-002 maps to one P19 contract/manifest; schema and mapping validators. |
| AC02 | P11 DID ownership only; minimal locator candidate followed by exact RLS revalidation, no independent ownership directory. |
| AC03 | P18 placement/admission consumed; no second placement model. |
| AC04 | Suspended/inactive placement blocks new dialogs; C02/C03. |
| AC05 | PostgreSQL bounded target state; DB constraints and adversarial input tests. |
| AC06 | One active target per Cell; direct DB violation and C06. |
| AC07 | Revision/idempotency fencing; C07/C22 and zero-row stale-writer proof. |
| AC08 | No tenant DB credentials/access in edge image/config/environment; secret/config and role tests. |
| AC09 | No open relay; C09/C10 plus unsolicited route-set tests. |
| AC10 | Forged internal headers grant zero authority; C08/C19. |
| AC11 | Unknown DID zero route; C04 and received-INVITE count. |
| AC12 | Missing active target zero route; C05. |
| AC13 | Resolver/DB failure zero new route; C11/C12. |
| AC14 | Two edges equivalent authoritative independent routing; C01/C20. |
| AC15 | Existing dialogs pinned across target/placement changes; C14/C15, real BYE/re-INVITE. |
| AC16 | P11 call lifecycle, ARI, number/account and idempotency regressions unchanged. |
| AC17 | P12 media/provider/handoff regressions unchanged; no media proxy introduced. |
| AC18 | No Organization relocation; explicit cross-Cell control request rejection. |
| AC19 | No automated failover; unavailable-target/owner-loss tests. |
| AC20 | No capacity certification; executable scope/documentation contract check. |
| AC21 | Native `kamailio -c` validation passes; invalid config fails deterministically. |
| AC22 | Immutable Kamailio version/image digest verified; build provenance and no floating tags. |
| AC23 | Real SIP INVITE/ACK/CANCEL/BYE/re-INVITE and retransmission tests, not Python mocks alone. |
| AC24 | Real independent PostgreSQL sessions/barriers prove placement/target races, no sleeps as proof. |
| AC25 | URI injection/relay/forged authority matrix with zero unintended target traffic. |
| AC26 | All canonical gates, exact-head CI/Security and clean-room evidence before closure. |
| AC27 | Docs distinguish P19 correctness from P25/P28/P29/P32 capabilities. |
| AC28 | Forced RLS/composite FKs/platform control and bounded bootstrap; C18/C21/C25. |
| AC29 | Route authorization/issue ordering, response loss and no duplicate cross-edge issue; C12/C13/C23. |
| AC30 | Target drain/retire safety and route history atomicity; C22–C24. |
| AC31 | Scalable indexed DID discovery; unique E.164/exact source FK and deleted/recreated locator identity tests. |
| AC32 | P11 source + locator synchronization in one transaction for every mutation path; injected rollback and C28. |
| AC33 | Final forced-RLS P11 revalidation of exact IDs/E.164/verified/inbound/account/Organization and locator revision; C27. |
| AC34 | No tenant scan: query-count/plan assertions independent of carrier tenant count, including missing DID. |
| AC35 | Locator-specific least privilege; actual non-superuser/non-BYPASSRLS roles, no SECURITY DEFINER or unrestricted tenant SELECT. |
| AC36 | Kamailio has no SQL access/credentials, including locator credentials; config/grant/secret inspection. |
| AC37 | Real shared-carrier fixture exceeds 32 Organizations/accounts without profile tenant enumeration; C26. |
| AC38 | Opaque server-only permit bound to durable P11 call; forged/public input denied and token absent from logs/events/LLM contexts. |
| AC39 | ARI channel-variable → operations-owned PJSIP header propagation proven on real integration; P11 public semantics/regressions unchanged. |
| AC40 | Authenticated Asterisk AND permit required; wrong peer/Cell, IP-only and Caller-ID-only attempts denied; C30/C31. |
| AC41 | Atomic one-time consumption, DB-time TTL and persistent per-call fence; C29 plus real SIP retransmission/dialog counts. |
| AC42 | Injected lost response after consume commit retains exact state and creates zero new grant/permit/carrier choice; C32. |
| AC43 | Exact approved upstream ID/revision/destination binding, rotation race and replay against another upstream denied; C31. |

## 16. Future implementation test and artifact strategy

Use SIPp or a strict real SIP client → two isolated Kamailio edges → authenticated
NXS resolver → real PostgreSQL → two SIP UAS targets representing Asterisk Cells.
A test UAS is sufficient for signaling correctness, not live production Asterisk
certification. Include UDP and TCP/TLS cases for every claimed supported transport,
valid TLS identity and negative peer tests; no mocked-only routing certification.

Reference baseline: Kamailio 6.1.x, specifically 6.1.4 verified against the official
[release announcement](https://www.kamailio.org/w/2026/08/kamailio-v6-1-4-released/).
Implementation must pin immutable version/source and image manifest digest, verify
trusted upstream/build provenance and record architecture-specific digests. No digest
is fabricated during governance. No `latest`, floating tag or unverified third-party
image. Validate required modules against that exact artifact, not another version.

Protocol references: official [TM 6.1](https://www.kamailio.org/docs/modules/6.1.x/modules/tm.html)
and [Dialog 6.1](https://www.kamailio.org/docs/modules/6.1.x/modules/dialog.html).
Use native transaction/record-route/dialog facilities, with authenticated pinning
checks around them; these modules alone are not tenant authorization or cross-node
exactly-once. Implementation must verify topology-hiding and RR compatibility with
the pinned build using real wire traffic, including both ACK classes and CANCEL.

Require native config validation, startup failure for invalid/missing trust/limits,
non-root image, health/readiness, shutdown, amd64/arm64 where supported, pinned
dependency/security/secret/container scans, disposable clean-room and full P11/P12/
P18 regression. Preserve existing >=90% coverage without exclusions. Real races use
independent sessions and explicit barriers/DB lock assertions; count actual UAS
dialog creations separately from legitimate SIP packet retransmissions.

Add a real ARI/Asterisk-PJSIP permit-propagation fixture for AC39; SIP UAS-only
tests cannot prove channel-variable delivery. Assert received initial INVITEs
separately from retransmissions and count permit grants. Locator tests require
raw SQL uniqueness/FK/role attacks, wrong tenant/number/account, disabled account
and number, revocation/deletion, transactional failure injection and >32 tenant
fixtures. C28/C29 require independent PostgreSQL sessions and deterministic barriers.

## 17. Migrations, rollout and rollback

Governance creates no migration. Future implementation adds P19 structures and the
minimal additive P11 source-binding constraints/internal synchronization seams,
following current Alembic head and schema guard; never edits deployed P11/P18 history.
Require fresh upgrade, canonical-schema upgrade, one head, `alembic check`, runtime
role attacks and disposable downgrade/re-upgrade. Do not backfill default targets,
peer trust, DID ownership or placements. Existing P11/P18 APIs keep their contracts.

Existing numbers require an explicit auditable, idempotent locator backfill using
bounded keyset batches under each trusted tenant context. Install synchronous P11
maintenance before backfill; each batch revalidates/locks source then locator and
cannot overwrite a newer revision. No fabricated ownership or active defaults.
Absent projections deny routing until provisioned; no carrier-wide discovery scan
on requests. Rollback must disable new ingress/egress first and preserve consumed
permit fences/history; dropping locator maintenance while routing remains enabled
is prohibited. Test all grants, source FKs and synchronization on upgrade/re-upgrade.

Reference setup is explicit/bounded: grant platform control, register validated
target/profile, verify P11 account/number ownership and P18 placement, activate target,
then enable test edge ingress. Empty state fails closed. Production rollout requires
separate P32 authorization. Before rollback, stop new dialog admission, drain exact
issued/dialog references and preserve audit/receipts; uncertain calls prevent silent
target deletion. Disposable schema downgrade is not a safe production rollback.
Prefer reviewed roll-forward; no call or media migration as a rollback shortcut.

## 18. Non-scope and Definition of Done

Excluded: Organization cross-Cell relocation/failover, sharding, media/RTP anchoring,
RTPengine, transcoding, live call/media migration, lossless edge failover, automatic
carrier failover, DR, Nomad/fleet orchestration/autoscaling, frontend, production
infrastructure/deployment, and unsupported calls-per-second/dialogs/Organizations-
per-Cell/SLA/RTO/RPO claims. P20 Sentinel, P21 compliance, P22 full audit, P23 cost,
P24 global observability, P25 recovery/reconciliation/stale-owner handling/failover,
P26 DR, P27 final hardening, P28 capacity, P29 chaos and P32 production remain separate.

Historical governance Definition of Done: schema/mapping/preflight/control checks pass, documents align with
main through P18, P19 remains PLANNED/PENDING with empty evidence, no lock/start,
one governance commit and exact-head CI/Security, then external architecture audit.
Implementation Definition of Done: every C01–C32 and AC01–AC43 has executed evidence, all
31 manifest gates pass, artifact/config provenance and real SIP/PG proofs exist,
separate implementation/closure commits follow authorized lifecycle and external
review. Governance test success is not runtime acceptance or permission to start.

## 19. Historical initial governance validation record

At baseline verification, local HEAD, origin/main and the existing remote P19 branch
all equaled `e2114cfe8f150e85b9ae432a9af557ceb52cf836`; the tree was clean. Checkout
created only a local tracking branch for that already-existing remote branch.
Initial and post-authoring `make nxs-validate-repo` and
`make nxs-preflight PHASE=NXS-P19` returned exit 0 / PASS (guard AUTHORIZED).
`make lint` returned exit 0 (693 files formatted, Ruff checks passed); `make type`
returned exit 0 (315 source files). The existing control/lifecycle/handoff suite
(`test_control_core`, `test_control_cli`, `test_lifecycle`, `test_agent_handoff`)
returned 52 passed in 5.70 seconds with `--no-cov -q`. `git diff --check` passed.
These are governance/control results, NOT P19 SIP runtime test results.

The protected-path diff against baseline is empty for source, migrations, tests,
scripts, workflows, compose, dependency files, project/registry/readiness/lock state
and existing evidence. No lifecycle start, lock acquisition or closure was invoked.
The requirement description alone changed; status/dependencies/mapping and empty
evidence remain intact. All semantic audit answers prohibiting duplicate placement,
new tenant authority, Kamailio DB access, P11/P12 redesign, open relay, forged header
authority, arbitrary tenant target hosts, failover, migration, capacity and deployment
are NO. P18 canonical on main and P19 PLANNED/PENDING are YES. Exact governance-head
CI/Security must be verified after commit/push; no pre-push PASS is asserted here.

## 20. Historical external architecture audit corrective #1

The preceding record describes initial governance, not architecture approval.
External review of `a2f8db2d2aeb602fbbab76ed71fe809f74233543` returned NO-GO:
bounded account enumeration did not solve shared-carrier discovery, and outbound
correlation lacked an explicit permit/ARI propagation mechanism. Sections 4, 6–8
and 10 now define the locator and egress permit contracts; C26–C32 and AC31–AC43
add future executable obligations. This is a governance correction only; none of
these new runtime proofs has been executed. External re-audit remains required.

Corrective local validation: repository/schema validation and P19 preflight passed
(exit 0, AUTHORIZED); lint passed (693 formatted files, Ruff clean), typing passed
(315 source files), and the same existing control/lifecycle/handoff suite passed
52 tests in 5.99 seconds. Protected source/migrations/tests/scripts/workflows,
dependency and all `.nxs` files remain unchanged from the audited governance HEAD.
README was reviewed: canonical main through P18 and P19 planned wording remains
accurate. Requirement description/dependencies and manifest need no change.
These results certify governance consistency only, not implementation behavior.
