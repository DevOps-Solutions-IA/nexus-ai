# NXS-P21 — Compliance Controls

Accepted admission design; P21 is now BUILDING/PENDING on its feature branch after
the single canonical start at 2026-09-27T22:26:51Z. Nothing in this document alone is
certification evidence. P21 is not validated, merged or deployed.
Requirement: NXS-COMP-001. Branch: feat/nxs-p21-compliance. Registry dependencies: P03 and P06.
Requirement dependencies explicitly bind P06 customer authority (NXS-CUSTOMER-001)
and P04 transactional outbox authority (NXS-EVENT-003), alongside the existing auth/tenant requirements.
Canonical baseline: 6d0dc0042c10016c0307aeda333b5fd89a7c3a54, P20 PR #46 merged;
post-merge CI 36343873102 and Security 36343873074 succeeded. P21 had not started at
that merge. No production deployment has occurred.

## Authority and security boundary

ADR-0102 governs. PostgreSQL is authoritative. P03 authenticates and revalidates live
session/user/membership/Organization state. Explicit RBAC is checked at the service
boundary as well as HTTP; caller JSON never supplies organization or approver identity.
Every P21 table is tenant-owned, forced RLS, organization-indexed and uses tenant-safe
foreign keys. Runtime is nexus_runtime, migration is nexus_migration. Sentinel has no
compliance authority. No system tenant, privileged database runtime or cross-tenant API.

Capabilities: compliance:read, compliance:manage_policy, compliance:manage_hold,
compliance:manage_request, compliance:approve, compliance:execute. Owner receives all;
admin receives read/manage_request only; ordinary members receive none. Revoked/inactive
membership and expired/revoked sessions fail closed. Approval identity comes from the
validated principal, never a payload. Policy activation and hold release persist that
principal and decision time; action approvals are separate durable rows.

## Policy, classification and retention

One Organization policy scope has versioned revisions DRAFT → ACTIVE → RETIRED.
Activated content is immutable; retirement changes state only. Activation retires the
previous revision atomically. A partial unique index prohibits two ACTIVE revisions.
All decisions bind the exact revision. A policy epoch changes on activation/retirement.

Source-defined resource classes distinguish PERSONAL, ORDINARY, CREDENTIAL and
OPERATIONAL data. No dynamic table name, import, SQL, URL, provider operation or handler
is accepted. Rules are strict bounded typed objects, with finite retention days and
RETAIN/ANONYMIZE/DELETE decisions. Unsupported adapter/action combinations fail closed.
Evaluation uses the stored target creation time and a trusted evaluation time; it does
not execute. Destructive execution is a separate approved, fingerprint-bound operation.
Credential classes are never exportable through generic subject requests.

## Legal holds

ACTIVE → RELEASED only through explicit privileged action. No inferred expiry.
Holds apply to an entire subject or an allow-listed resource class for that subject.
Every create/release increments the Organization hold epoch. Applicable ACTIVE holds
block all destructive actions. Planning checks holds; final execution rereads holds
under the same Organization control lock as the local domain mutation. A changed hold
epoch invalidates an old approval even if the new hold has subsequently been released.

## Subject requests and coverage

ACCESS, ERASURE and RESTRICTION are supported request types. Subject identity is a
tenant-visible P06 customer UUID, not arbitrary text/SQL. Creation is idempotent under
(organization, idempotency key); a different payload with the same key conflicts.

RECEIVED → VERIFIED → PLANNED → APPROVED → EXECUTING → COMPLETED/PARTIAL.
DENIED/CANCELLED/EXPIRED are terminal alternatives before execution. There is no blind
reopen or terminal-to-success transition. Verification requires manage_request and
stores only a bounded method code/opaque evidence reference, verified_by and DB time.
No uploaded identity document or raw identity-proof secret is retained.

The fixed inventory is part of coverage, not a user-supplied list that can hide a
material domain. Unsupported subject resources remain visible in results. Even a
successful profile-only action is PARTIAL for a whole-subject request. New unsupported
coverage discovered before completion cannot be dropped to produce COMPLETED. Exports
are bounded allow-listed profile fields, never arbitrary row serialization or secrets.
Retention is resource-scoped; it does not imply whole-subject erasure.

## Inventory inspected before implementation

The table groups below enumerate current persistent tenant domains from model registry
and subsystem models. SUPPORTED denotes the adapter committed to by this design, not
an already-certified implementation. No existing subsystem exposes universal erasure.
All deferred subject-relevant groups contribute incomplete coverage. Holds apply to
future destructive adapters before those adapters can be enabled. Platform objects
carrying diagnostic organization metadata are not tenant authority.

| Resource/domain (tables) | Owner / tenant ownership | Sensitivity | Existing mutation / read primitive | Retention / hold interaction | P21 adapter |
| --- | --- | --- | --- | --- | --- |
| Customer profile (`customers`) | P06 / organization | PERSONAL display name, locale | CustomerRepository insert/by_id; new P06-owned bounded anonymize/restrict primitive required | No prior retention; profile destructive mutation under P21 hold fence | SUPPORTED (profile only) |
| Identities (`customer_identities`) | P06 / organization | PERSONAL phone/email/external identity | Identity repository link, resolve, verification/revoke | Canonical identity uniqueness and downstream references preclude generic deletion | DEFERRED |
| Conversations (`conversations`, `conversation_participants`, `conversation_activities`) | P06 / organization | PERSONAL content/references | Conversation service close/reopen, timeline reads | History and downstream references retained; not erased by profile mutation | DEFERRED |
| Organization (`organizations`, `organization_settings`, `dashboard_configurations`, `provisioning_requests`) | P02/P05 / organization or provisioning boundary | ORDINARY and personal legal/contact metadata | Organization lifecycle/profile, provisioning repositories | Archival is not erasure; no retention adapter | DEFERRED |
| Membership/auth (`memberships`, `role_assignments`, `refresh_sessions`) | P03 / organization | PERSONAL/security; token hashes excluded | Membership/role administration, session revocation | Security records cannot be generically erased/exported | DEFERRED |
| Credentials (`integration_secrets`, `messaging_secrets`, `telephony_secrets`, `voice_secrets`, `ai_model_secrets`) | P07/P09/P11/P12/P13 / organization | CREDENTIAL encrypted secrets | Dedicated vault/credential boundaries only | Generic subject export forbidden; no P21 credential adapter | NOT_APPLICABLE |
| Events (`event_outbox`, `event_dead_letters`) | P04 / organization | OPERATIONAL, envelopes can contain personal metadata | Transactional enqueue, relay, controlled replay | Reliable delivery/history, not a generic erasure API | DEFERRED |
| Integration config (`integrations`, `integration_operations`, `webhook_endpoints`) | P07 / organization | ORDINARY/credential references | Integration service bounded configuration/read | No retention primitive; URLs/credentials never executable P21 selectors | DEFERRED |
| Integration history (`integration_execution_records`, `integration_idempotency_records`, `webhook_receipts`) | P07 / organization | OPERATIONAL, possible personal results | Execution/idempotency/replay repositories | Identity receipts cannot be removed to permit duplicate effects | DEFERRED |
| Tools (`tool_definitions`, `tool_execution_records`, `tool_idempotency_records`) | P08 / organization | ORDINARY/OPERATIONAL | ToolEngine controlled invocation/read | P21 cannot invoke arbitrary tools or erase execution fences | DEFERRED |
| Messaging (`messaging_accounts`, `messaging_messages`, `messaging_inbound_receipts`, `messaging_send_idempotency`) | P09 / organization | PERSONAL content/address; account secret references | Messaging service send/read, provider status | External delivery and provider copies not erased by local changes | DEFERRED |
| OTP (`otp_challenges`) | P10 / organization | CREDENTIAL hashes and personal destination | OTP issue/verify/revoke | Verification expiry is not subject erasure | NOT_APPLICABLE (generic export forbidden) |
| Telephony (`telephony_accounts`, `telephony_phone_numbers`, `telephony_calls`, `telephony_call_events`, `telephony_media_sessions`) | P11 / organization | PERSONAL addresses, OPERATIONAL/media references | Telephony configure/originate/hangup/status | No provider deletion authority or media erasure primitive | DEFERRED |
| Voice (`voice_provider_accounts`, `voice_profiles`, `voice_sessions`, `voice_provider_events`, `voice_usage_records`) | P12 / organization | PERSONAL/OPERATIONAL/provider references | Voice session/configuration boundary | No remote recording erasure authority | DEFERRED |
| Agent config (`ai_model_provider_accounts`, `ai_model_profiles`, `ai_agents`) | P13 / organization | ORDINARY, prompt/credential references | Agent/model configuration service | No generic secret export | DEFERRED |
| Agent runtime (`ai_agent_sessions`, `ai_agent_turns`, `ai_agent_tool_calls`, `ai_model_usage`, `ai_agent_model_dispatch_permits`, `ai_agent_tool_dispatch_permits`) | P13 / organization | PERSONAL prompts/responses, OPERATIONAL fences | Agent runtime/session reads and transitions | Live turns and dispatch receipts must not be generically deleted | DEFERRED |
| Workflows (`workflow_definitions`, `workflow_versions`, `workflow_version_steps`, `workflow_runs`, `workflow_step_runs`, `workflow_transition_history`) | P14 / organization | ORDINARY/possibly PERSONAL input/output | Versioned workflow service, fenced steps | Published versions immutable, no generic erasure of live execution | DEFERRED |
| Scheduler (`scheduler_schedules`, `scheduler_occurrences`, `scheduler_transition_history`) | P15 / organization | OPERATIONAL/possibly PERSONAL inputs | Schedule/cancel/claim service | Temporal authority and occurrence identity retained | DEFERRED |
| Campaign config/audience (`campaigns`, `campaign_revisions`, `campaign_audience_snapshots`, `campaign_recipients`) | P16 / organization | PERSONAL destinations, ORDINARY configuration | Campaign preparation and recipient service | P21 cannot redefine audience/send eligibility | DEFERRED |
| Campaign execution (`campaign_runs`, `campaign_recipient_attempts`, `campaign_send_permits`, `campaign_throttle_windows`, `campaign_organization_throttle_windows`, `campaign_transition_history`) | P16 / organization | OPERATIONAL, personal references | Campaign dispatch/permit/throttle boundary | Stable send identity and history not generically erased | DEFERRED |
| Campaign consent (`campaign_contact_preferences`, `campaign_suppressions`, `campaign_policy_epochs`) | P16 / organization | PERSONAL consent evidence | P16 contact preference/suppression authority | Profile restriction does not claim campaign restriction or alter consent | DEFERRED |
| Human operations (`human_queues`, `human_agent_presence`, `human_work_items`, `human_assignments`, `human_handoffs`, `conversation_ownership`, `human_action_authorizations`, `human_transition_history`) | P17 / organization | PERSONAL/OPERATIONAL | Human assignment/ownership/handoff services | Live ownership/generation fences remain authoritative | DEFERRED |
| Placement (`organization_placements`, `placement_mutations`) | P18 / organization | OPERATIONAL | Placement service admission/mutation | Not customer erasure; no P21 relocation authority | NOT_APPLICABLE |
| SIP tenant routing (`sip_account_upstreams`, `sip_account_upstream_history`, `sip_did_locators`, `sip_route_authorizations`, `sip_dialog_bindings`, `sip_route_history`, `sip_egress_permits`, `sip_egress_routes`, `sip_egress_history`, `sip_egress_dialog_bindings`, `sip_call_admissions`) | P19 / organization | PERSONAL destinations/OPERATIONAL fences | SIP route/permit admission | Dialog/permit identity cannot be erased by generic compliance actions | DEFERRED |
| Platform identity/catalog, Cell/SIP infrastructure, consumer receipts and all `sentinel_*` tables | P03/P04/P18/P19/P20 / platform, not tenant-owned | CREDENTIAL/OPERATIONAL/advisory metadata | Existing platform services only | No tenant P21 authority over platform rows | NOT_APPLICABLE |

## Execution, fingerprints and linearization

An Organization control row is inserted with database unique identity and locked FOR
UPDATE before any P21 mutation. No process lock supplies authority. Policy and hold
epochs belong to that row. Commit determines ordering for all following races.

1. Activation: control lock → revision lock; retire old and activate new in one commit.
2. Planning: control → verified request → active policy → hold query → domain target;
   persist immutable plan and SHA-256 of canonical sorted JSON. Bind organization,
   request/subject, policy identity/revision/epoch, resource/action, target/version,
   hold epoch, parameters and stable operation identity.
3. Approval: same order, fresh principal/RBAC, exact plan fingerprint and finite DB-time
   expiry. A semantic change does not update an approved plan; it requires a new plan.
4. Claim: control → request → plan → approval → execution; one slot per plan, DB-time
   lease, owner and monotonic generation. Only expired pre-effect claims are recoverable.
5. Effect: control → request → plan → approval → execution → target; reread policy,
   holds, approval expiry, fingerprint, owner/generation/lease and target version. The
   supported P06 local mutation and execution/result/outbox commit atomically.

No database transaction spans provider/network I/O. No external action adapter is
enabled in P21; external dispatch and provider ambiguity handling are not implemented
or certified. Unsupported external operations fail closed before any dispatch. The
execution schema reserves AMBIGUOUS, but no external adapter can enter that state in
this release. A lost response after a local commit is recovered by replaying the same
durable receipt, never by creating a new operation identity. Pre-effect failures roll
back and leave the existing claim fenced; database or outbox failure also rolls back
the local effect. Successful replay returns the same durable result. Lease expiry is
checked again after acquiring the target lock, before mutation; stale workers cannot
finish a newer generation.

## API, events and operational bounds

Versioned `/api/v1/compliance` endpoints use strict schemas, trusted tenant resolution,
live principals, permissions, bounded page sizes/cursors and RFC 9457 errors. There is
no body organization_id, approver id, revision override, SQL, table, handler or URL.
Inputs select opaque resource UUIDs, never executable selectors.

Typed P04 events announce policy/hold/request/action transitions inside the mutation
transaction. Payloads contain IDs, enum states and safe reason codes only; no exports,
raw identity proofs, secrets or arbitrary exception text. This is not P22's audit ledger.

Finite limits apply to rules, resource lists, request bytes, metadata, pagination,
export bytes, lease duration, approval expiry and work batches. No unbounded fanout,
automatic background erasure, provider I/O or infinite retries. Metrics/logs expose
safe IDs/reason codes, not personal exports. Rollback uses disposable migration
downgrade only during validation; production rollback requires separate authorization.

## Certification contract

Real PostgreSQL deterministic transaction/barrier tests must prove:
C01 activation race; C02 retirement/planning; C03 revision/approval; C04 hold/planning;
C05 hold/final effect; C06 hold release/effect; C07 duplicate request; C08 approve/deny;
C09 cancel/approve; C10 cancel/claim; C11 duplicate claim; C12 expired lease takeover;
C13 stale completion; C14 duplicate retry; C15 target drift; C16 same subject/two tenants;
C17 forged tenant resource; C18 completion/new unsupported coverage; C19 retention/hold;
C20 outbox failure/commit. No mocked database authority or sleep-only synchronization.

Acceptance map must prove AC01 requirement; AC02 manifest; AC03 tenant persistence;
AC04 forced RLS; AC05 trusted context; AC06 RBAC; AC07 policy revisions; AC08 immutable
activation; AC09 activation race; AC10 bounded retention; AC11 allowlist; AC12 no generic
SQL; AC13 hold precedence; AC14 final hold reread; AC15 request transitions; AC16 subject
verification; AC17 unsupported fail-closed; AC18 truthful partial; AC19 fingerprint;
AC20 stale policy; AC21 stale target; AC22 durable approval; AC23 idempotency; AC24 claim
fencing; AC25 stale completion; AC26 ambiguity; AC27 no transaction across external I/O;
AC28 atomic outbox; AC29 no P22 duplication; AC30 no P16 duplication; AC31 no model
authority; AC32 credential exclusion; AC33 tenant attacks; AC34 migration roundtrip;
AC35 containers; AC36 regression/coverage; AC37 exact-head CI; AC38 exact-head Security;
AC39 accurate docs; AC40 no regulatory/production claim.

Security/resilience tests cover malformed/oversized input, unknown or SQL/shell/URL-shaped
selectors, forged org/approver, inactive membership, stale token, hold bypass, repository
bypass, wrong fingerprint/revision, expired approval, missing verification, target drift,
unsupported adapter, bounded export, crash before/after claim and local commit,
database unavailability and outbox failure. External ambiguity is excluded by the
absence of an external dispatch adapter, not simulated as a certified provider flow.
No skips or weakened thresholds.

Evidence belongs under `.nxs/evidence/NXS-P21/`, with actual commands/results/source
binding, AC01–AC40 and C01–C20 mappings. Admission is not proof of implementation.
Stage A ends BUILDING/PENDING, NXS-COMP-001 IN_PROGRESS, implementation/closure commits
null, exact-head CI/Security green and independent implementation audit pending.

## Non-scope

No P22 audit ledger, P23 billing/metering, P24 global observability, P25 recovery/failover,
P26 backup/DR, P27 final hardening, P28 capacity, P29 chaos, P30 backend certification,
P31 release, P32 deployment, frontend, legal advice, legislative interpretation,
regulatory certification, external compliance SaaS authority, cross-tenant compliance
administration or model authority. No GDPR/HIPAA/SOC 2/ISO 27001/PCI certification or
jurisdictional compliance is claimed. Deferred resources are not implemented adapters.
