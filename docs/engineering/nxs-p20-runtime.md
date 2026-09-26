# Sentinel runtime — feature-branch implementation

P20 remains BUILDING/PENDING. This implementation is not merged, closed or
production deployed. Publication requires current-source local certification and
exact-head CI/Security; external implementation audit remains a separate gate.

## Authority and composition

`SentinelDatabase` connects exclusively as `nexus_sentinel`, verifies both database
identities and privilege flags on every transaction, and never sets tenant scope.
Operations compose `SentinelStore`, `SignalCollector`, `SentinelReasoner`,
`SentinelRunbookRegistry`, `SentinelActionExecutor` and `SentinelControl` explicitly;
imports create no connections, background workers or model requests. The internal
control facade is not mounted as a tenant HTTP API. It authenticates existing signed
tokens/live sessions and requires an explicit `sentinel:*` platform grant. The
existing authentication database is used only for authentication/authorization;
Sentinel state never falls back to its runtime role.

Registered observation probes cover NXS state, dedicated PostgreSQL connectivity,
NATS/JetStream, P18 inventory/placement and P19 Asterisk signaling-target state.
The latter reports registry health, not media availability or call capacity.
Optional HTTP health uses fixed operations-owned HTTPS endpoints through P07's
SSRF-checked, address-pinned executor, with redirects disabled. Input signals cannot
choose a destination or principal. All probes run within bounded adapter timeouts;
external error strings and payloads are discarded rather than persisted.

## Linearization and history

Source observation uniqueness linearizes receipt insertion. An active-correlation
partial unique index and incident row lock converge new observations. Receipt and
incident updates share a transaction. A resolution racing insertion either precedes
a new incident or yields an explicit retryable conflict; no observation is silently
attached to a terminal incident. Severity escalates monotonically within an incident.
Only a latest trusted HEALTHY receipt in MONITORING or an authorized operator resolves
an incident. Later observations create new history, never resurrect a resolved row.

Findings append under the incident lock. Runbook revisions are immutable and registration
serializes with the durable control row. Proposal convergence uses incident locking
and a unique SHA-256 semantic fingerprint. New semantics produce another proposal;
old approval is not transferable. Policy and kill-switch mutations use revision CAS.

## Reasoning and secrets

Reasoning is advisory and disabled by default. The credential provider loads a bounded
read-only, non-symlink, single-link file owned by root or the service user from a
protected directory. It does not use tenant vaults. The provider-neutral P13 adapter
is used only as a request/response normalizer over a fixed-origin governed transport.
Credentials appear only in the transport authentication header, never model content;
request repr and surfaced exceptions are redacted. No live-provider certification is
claimed. Tests exercise the actual serialization contract with deterministic responses.

The reasoner accepts typed facts and evidence IDs, returns strict structured findings
and optional registered runbook suggestions, and has neither a tool loop nor an
execution reference. Unknown fields, handlers, evidence IDs and executable inputs fail
closed. No P04 events are emitted; no synthetic Organization is created for events.

## Execution fence

Lock order is incident, proposal, policy/approval snapshot, execution slot. The policy
row serializes capacity checks across different proposals. One permanent slot per
proposal stores owner, generation, DB-time lease and stable `sentinel:<proposal_id>`
idempotency identity. Source binding includes the exact target tuple and generation;
changing a source target binding requires a new immutable runbook revision. Production
adapters are read-only diagnostics. Reversible effects are exercised only by bounded
test adapters, never arbitrary infrastructure mutations.

The final transaction validates current incident/runbook/policy/approval semantics,
source target binding, owner, generation, lease and kill switch, then commits
DISPATCHED before external I/O. This commit is the authorization linearization point:
a later policy change does not undo an already-granted dispatch. No transaction spans
external I/O. A proven pre-effect rejection is FAILED; a possible effect or unknown
response is AMBIGUOUS. A dispatched slot is never reclaimed, even after expiry.

Only expired CLAIMED slots may transfer ownership using a new generation. Cleanup
is bounded and locks/rechecks rows: expired pre-dispatch claims become FAILED, expired
DISPATCHED slots become AMBIGUOUS. Cleanup never deletes the idempotency fence or
reconciles external effects. P25 failover and reconciliation remain out of scope.

## Reproduction

Use `make db-bootstrap`, `make migrate-check`, `make nxs-schema-guard`, and
`uv run pytest tests/unit/test_sentinel* tests/contracts/test_sentinel_scope.py
tests/integration/test_sentinel*`. Real PostgreSQL tests include migration round trips,
role isolation, native service probes, operator grants and execution races. Full
certification additionally uses the repository clean-room, security and container
gates; historical foundation evidence alone does not certify this implementation.

Additive revisions are `d20a0b1c2d3e`, `d20b0b1c2d3e` and `d20c0b1c2d3e`.
The last revision indexes active execution expiry and latest-receipt lookup, without
changing historical migrations. A downgrade on populated
state can refuse when repeated resolved correlations or new platform grants cannot
fit the old schema; it never silently deletes history to force downgrade. Disposable
fresh/P19 upgrade, downgrade and re-upgrade tests are the supported certification path.
