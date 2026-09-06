# NXS Engineering Control System

`.nxs/project-state.json` is the execution summary; the phase registry defines ordering; manifests bind scope, requirements and gates; the requirements ledger preserves product obligations; readiness records and evidence prove closure. JSON Schemas reject malformed values, while `scripts.nxs_control` enforces relationships that schemas cannot express, including unique logical IDs, READY/GO consistency, dependency readiness and complete mandatory mappings.

The execution guard validates state before work and blocks repeated READY phases, wrong branches, failed dependencies, conflicting active phases and stale locks. Lock acquisition records phase, branch, actor, HEAD and expiry. Only an expired lock on a clean tree can be recovered.

The gate engine records exact commands, exit codes and bounded output in `.nxs/evidence/<phase>/quality-gates.json`. Closure requires VALIDATING state, PASS evidence, satisfied mapped requirements, and a full implementation SHA. Closure changes state to READY/GO and opens the dependency-qualified next phase. Commit the implementation first, run gates, execute closure with that SHA, then commit closure state and evidence. The closure commit cannot reference itself and is recorded externally by Git history/readiness follow-up.

Agent handoff is repository-only: the entering agent reads `AGENTS.md`, validates state, observes the allowed phase, and verifies evidence. No agent-specific memory is authoritative.
