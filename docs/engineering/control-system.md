# NXS Engineering Control System

`.nxs/project-state.json` is the execution summary; the phase registry defines ordering; manifests bind scope, requirements and gates; the requirements ledger preserves product obligations; readiness records and evidence prove closure. JSON Schemas reject malformed values, while `scripts.nxs_control` enforces relationships that schemas cannot express, including unique logical IDs, READY/GO consistency, dependency readiness and complete mandatory mappings.

The execution guard validates state before work and blocks repeated READY phases, wrong branches, failed dependencies, conflicting active phases and stale locks. Lock acquisition records phase, branch, actor, HEAD and expiry. Only an expired lock on a clean tree can be recovered.

The gate engine records exact commands, exit codes and bounded output in `.nxs/evidence/<phase>/quality-gates.json`. Closure requires VALIDATING state, PASS evidence, satisfied mapped requirements, and a full implementation SHA. Closure changes state to READY/GO and opens the dependency-qualified next phase. Commit the implementation first, run gates, execute closure with that SHA, then commit closure state and evidence. The closure commit cannot reference itself and is recorded externally by Git history/readiness follow-up.

Agent handoff is repository-only: the entering agent reads `AGENTS.md`, validates state, observes the allowed phase, and verifies evidence. No agent-specific memory is authoritative.

## Generic lifecycle (P01)

Normal admission must select exactly `next_eligible_phase()`. An explicit phase argument
does not authorize skipping an earlier eligible phase; diagnostics name requested and
expected phases. Active-phase continuation remains valid under its guard, and the
controlled READY → VALIDATING corrective reopen still owns the active execution slot.

## Deferred obligations and certification policy (H0-01)

`.nxs/deferred-obligations.json` and its schema are the durable deferral authority.
Each obligation names its source phase, concrete owner, category, severity,
certification impact, blocking phases, proof expectation and source references.
Status history begins OPEN; legal transitions are OPEN → IN_PROGRESS/RESOLVED,
IN_PROGRESS → OPEN/RESOLVED and RESOLVED → OPEN for a reopened obligation.
Open entries cannot carry resolution data. Resolution requires the owner READY/GO,
its actual Git implementation commit and existing evidence bound in its manifest.
Verification readers require the owning commit objects in local Git history; absent
history fails closed rather than accepting an unverifiable resolution.
No description or future promise substitutes for completion evidence.

The registry's certification policy declares P30's required P22 ancestor and requires
every production blocker to name P30 in `blocks_phases`. Each production-blocking owner
must precede certification in the dependency DAG. Generic validation rejects cycles,
unknown/duplicate identifiers, invalid histories, premature resolution, graph weakening
and READY with unresolved blockers. Guard and closure/READY prechecks fail before state
writes. The added obligations target future certification; P00-P21 historical readiness
and evidence are unchanged. H0 does not start a phase or certify P22.

Choose durable correctness, security, scalability, recoverability and explicit authority
over short-term convenience. Every deferral needs a concrete owning phase. Never weaken
a gate to manufacture green. Concurrency ordering needs observable semantic barriers;
fresh dependency security applies to the actual candidate environment. See
`h0-governance-hardening.md` for future requirements and the admission boundary.

No phase identifier is hardcoded. `scripts.nxs_start` begins any eligible phase: it runs the guard, maps the phase's mandatory requirements to IN_PROGRESS, transitions PLANNED→READY_TO_EXECUTE→BUILDING, sets `current_phase` and `active_phase`, auto-creates the manifest when absent, and acquires the lock. `eligible_phases()` returns, in registry order, the phases that are not READY, not blocked, have every dependency at READY/GO, and do not conflict with a different active phase; `next_eligible_phase()` is the deterministic head of that list. Closure recomputes `next_allowed_execution` from that function against the just-updated in-memory registry and state, replaces the full `current_phase` identity, and releases the lock. The execution lock is operated through `python -m scripts.nxs_guard lock {acquire|status|release|recover}` with machine-readable status. CI detects its phase from the branch via `scripts.nxs_guard current-phase` and applies the same guard logic for `feat/nxs-*`, `fix/nxs-*` and `release/*`.

`READY` is terminal for normal execution; the only transition out of it is the controlled reopen `READY → VALIDATING` (`REOPEN_TRANSITION`), used for a corrective delta such as an audit finding. The reopen clears the phase's closure state — it removes the phase from `completed_phases`, clears the recorded implementation commit, drops the stale readiness entry, and restores the phase as `active_phase` — so the two-stage protocol (fresh implementation candidate + green CI, then closure) applies again to the corrected work.
