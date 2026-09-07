# Phase Lifecycle Runbook

## Start or continue

Read `AGENTS.md` and all canonical NXS files, fetch Git state, confirm the registered branch and run `make nxs-validate-repo` plus `make nxs-preflight PHASE=<id>`. Acquire a lock only after PASS. Another agent repeats these steps and relies solely on repository state and evidence.

## Recover a stale lock

Confirm the recorded expiry has passed, identify the previous actor/work, and require a clean working tree. Run the controlled recovery function; never delete or hand-edit the lock. A live lock or dirty tree requires coordination, not recovery.

## Failed gate

Keep the phase VALIDATING or move it to FAILED through the state command. Preserve the failing evidence, repair the root cause, rerun the complete gate, and never overwrite evidence with invented results or lower thresholds.

## Close and verify

Commit implementation and capture the full SHA. Mark P00 requirements IMPLEMENTED using reviewed state changes, transition BUILDING to VALIDATING, run the full gate, then run `make nxs-close PHASE=<id> IMPLEMENTATION_COMMIT=<sha>`. Review the generated READY/GO state and evidence, commit the closure changes separately, push, and require green GitHub checks before treating CI as proven. Verify a fresh reader sees the completed phase and dependency-qualified next phase. Do not begin that next phase during closure.
