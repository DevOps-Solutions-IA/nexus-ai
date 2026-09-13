# NXS-P14 Corrective 01 — Conditional Branch Propagation

## Audit finding and fail-first evidence

External review found that `_advance()` treated `COMPLETED` and `SKIPPED` as equivalent dependency success. The fail-first test `test_condition_skips_exclusive_unselected_branch_descendants` used `choose → no → no_child`, selected the opposite `yes` branch, and observed `no=SKIPPED` but `no_child=READY` on reviewed head `e835e5781506872789be4374dc4f8af90171994e`.

The exact focused command failed with one assertion failure before the production change:

```text
uv run pytest -q tests/integration/test_workflow_service.py::test_condition_skips_exclusive_unselected_branch_descendants
observed: no_child=READY
expected: no_child=SKIPPED
```

The command's isolated coverage failure was expected because the repository-wide coverage threshold applies to the full suite; the semantic assertion failed independently and reproduced the audit finding.

## Root cause and old semantics

The old advancement predicate promoted a pending step whenever every predecessor was either `COMPLETED` or `SKIPPED`. It lost the distinction between a successful predecessor and a predecessor excluded by a condition. The direct rejected branch root was durable, but exclusion did not propagate beyond that root.

## Formal semantics

For a pending step, evaluate the immutable dependency list against durable step-run states:

1. With no dependencies, the step is `READY`.
2. If any dependency is unresolved, the step remains `PENDING`.
3. If all dependencies are resolved as `COMPLETED` or `SKIPPED` and at least one is `COMPLETED`, the step is `READY`.
4. If every dependency is `SKIPPED`, the step becomes `SKIPPED`.

The repository applies this rule to a deterministic fixed point while holding the workflow-run lock and locking all step-run rows. State changes and transition history commit atomically. `SKIPPED` remains terminal and cannot return to `READY`, `RUNNING`, or `COMPLETED`.

## Convergence semantics

A convergence step waits until every incoming dependency is resolved. It proceeds once at least one incoming predecessor completed, so a selected branch can join with excluded alternatives. A node whose entire incoming frontier is excluded is itself excluded. The same rule handles simple joins, multi-level joins, shared descendants, and successors after a join without graph mutation or branch-specific process memory.

This rule defines dependency edges as a synchronization frontier across effective active paths. A completed non-branch predecessor therefore keeps a shared descendant reachable even when another incoming alternative was skipped.

## Durable reconstruction and replay

Condition output now records the boolean result plus bounded `selected_steps` and `skipped_steps` root lists. The immutable condition configuration, durable condition output, direct root states, descendant states, and append-only transition history are sufficient to reconstruct the chosen path after restart. Fixed-point advancement itself depends only on durable step states and immutable dependencies.

Duplicate completion with the same or opposite outcome is rejected by the existing active-state, owner, and opaque claim-token fence. Two workers serialize on the run row; only the current condition claim may persist an outcome, branch-root exclusion, descendant propagation, and readiness changes.

## Nested conditions and external-effect safety

A condition inside an excluded branch becomes `SKIPPED`, is never claimed, and cannot activate either child branch. Exclusion propagates through TOOL and AGENT descendants, proving zero P08 Tool Engine dispatches, zero tool-execution records, and zero P13 provider calls for inactive paths.

Cancellation after branch resolution uses the same run-row authority. It cancels remaining active-path pending/ready work without changing already skipped descendants, and no later advancement occurs after the run becomes terminal.

## Certification matrix

The corrective tests cover direct selection, multi-level and deep exclusion, simple and multi-level convergence, a successor after convergence, nested active conditions, a condition inside a skipped branch, skipped TOOL and AGENT descendants, opposite-outcome replay, an explicitly synchronized two-worker race, cancellation after resolution, and terminal `SKIPPED` absorption.

## Remaining boundary

This corrective changes no claim recovery behavior. Automatic orphan reaping, stale-lease reassignment, ambiguous external-effect reconciliation, failover, and redispatch remain exclusively within NXS-P25. P15 scheduling and every deployment concern remain untouched.
