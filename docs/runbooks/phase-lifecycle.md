# Phase Lifecycle Runbook

The control system is generic: no phase identifier is hardcoded. The next eligible phase
is computed from `.nxs/phase-registry.json` and dependency state — a phase is eligible
when it is not READY, not blocked, has every dependency at READY/GO and does not conflict
with a different active phase; ties break by registry order.

## Start or continue a phase

Read `AGENTS.md` and every canonical NXS file, fetch Git state, confirm the registered
branch, and run `make nxs-validate-repo` then `make nxs-preflight PHASE=<id>`. On PASS,
run `make nxs-start PHASE=<id> ACTOR=<agent>`: it re-checks the guard, maps the phase's
mandatory requirements to IN_PROGRESS, transitions PLANNED→READY_TO_EXECUTE→BUILDING,
sets `current_phase` and `active_phase`, creates the phase manifest if absent, and
acquires the execution lock. Re-running it on an already-active phase is a no-op.

## Execution lock

`make nxs-lock-status` (or `python -m scripts.nxs_guard lock status --json`) shows state,
holder, staleness and seconds remaining. `nxs-lock-acquire` / `nxs-lock-release` take
`PHASE=` (and acquire takes `ACTOR=`). A lock is released automatically by a successful
closure.

## Recover a stale lock

Only an expired lock on a clean working tree can be recovered:
`make nxs-lock-recover`. Confirm the recorded expiry has passed and identify the previous
actor's work first. A live lock or a dirty tree requires coordination, never recovery;
never hand-edit `.nxs/execution-lock.json`.

## Failed gate

Keep the phase VALIDATING or move it to FAILED with `python -m scripts.nxs_state`.
Preserve the failing evidence, fix the root cause, rerun the complete gate, and never
overwrite evidence with invented results or a lowered threshold.

## Close and verify

Stage A — implementation candidate: complete the work, run the full local gate, create
the implementation commit, push, open/update the PR and let GitHub CI validate it; repair
until green. Stage B — closure: transition BUILDING→VALIDATING with
`python -m scripts.nxs_state`, run `make nxs-gate PHASE=<id>` and the mandatory quality
matrix, then `make nxs-close PHASE=<id> IMPLEMENTATION_COMMIT=<sha>`. Closure verifies the
guard, that the SHA exists in Git, that gate and matrix evidence are all PASS, marks the
mapped requirements VALIDATED, sets the phase READY/GO, replaces `current_phase`,
releases the lock and recomputes `next_allowed_execution`. Commit the closure state
separately, push, and require green GitHub checks before treating CI as proven. Confirm a
fresh reader sees the completed phase and the dependency-qualified next phase, then stop.
