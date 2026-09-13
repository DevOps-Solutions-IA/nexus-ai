# NXS-P14 Corrective 02 — Explicit Dependency Semantics

## Audit finding and fail-first evidence

External review found that Corrective 01's global convergence rule could not distinguish a mandatory conjunction from an alternative branch join. On reviewed head `aa4a0f3d688bffd8150ba82de073e488fba59099`, the fail-first test `test_default_mandatory_dependencies_fail_closed_when_branch_is_skipped` selected one condition branch, completed `required_a`, skipped `required_b`, and observed the ordinary two-dependency `mandatory_join` as `READY` instead of `SKIPPED`.

```text
uv run pytest -q --no-cov tests/integration/test_workflow_condition_branches.py::test_default_mandatory_dependencies_fail_closed_when_branch_is_skipped
observed: mandatory_join=READY
expected: mandatory_join=SKIPPED
result: 1 failed
```

The root cause was semantic, not timing-related: one resolver treated every dependency set as alternative convergence whenever at least one predecessor completed.

## Immutable dependency contract

`WorkflowStepSpec` now contains the closed enum `DependencyMode.ALL | DependencyMode.ANY`. The field is frozen with the rest of the version step, rejects unknown values, and defaults to `ALL`. `ANY` requires at least two dependencies; zero- and single-dependency `ANY` declarations are rejected because they express no alternative convergence.

`workflow_version_steps.dependency_mode` persists the mode as an inspected column with a database check constraint. Migration `d6e7f8a9b0c1` chains from the original P14 migration and backfills/defaults existing P14 rows to `ALL`. Published step rows remain protected by the existing immutable-row trigger, so draft edits can produce a new version but cannot alter prior or active execution semantics.

## Resolution semantics

Every pending step waits while any dependency is outside `COMPLETED` or `SKIPPED`. `FAILED` and `CANCELLED` are not normal dependency satisfaction; workflow failure and cancellation control the run and prohibit downstream activation.

For `ALL`:

- every dependency `COMPLETED` results in `READY`;
- any `SKIPPED` dependency, after all dependencies resolve, results in `SKIPPED`;
- therefore `verify_identity=COMPLETED` plus `fraud_approval=SKIPPED` cannot release payment.

For `ANY`:

- at least one `COMPLETED`, after all dependencies resolve, results in `READY`;
- all dependencies `SKIPPED` results in `SKIPPED`;
- therefore a condition's completed selected branch can converge with its skipped alternative only when the join explicitly declares `ANY`.

No mode is inferred from dependency count, condition ancestry, names, or timing. The result is a pure function of immutable mode/list, durable predecessor states, and durable run state.

## Condition and concurrency interaction

Conditions continue to durably record `selected`, `selected_steps`, and `skipped_steps`. Direct rejected roots become terminal `SKIPPED`; fixed-point descendant propagation then evaluates each descendant's own immutable dependency mode. Nested conditions in excluded paths never execute. Simple and multi-level joins opt into `ANY`; mandatory downstream conjunctions retain the fail-closed `ALL` default.

The workflow-run row remains the serialization boundary. Step rows are locked and resolved transactionally, published mode cannot change, and the owner/token fence permits only one condition completion. Two workers therefore derive the same durable result without process-local locks. Replay cannot flip condition outcome or dependency mode. Cancellation prevents all later advancement, and `SKIPPED` remains absorbing.

## External-effect and security impact

The change adds no executable surface. The mode is a two-value enum; it enables no expression evaluation, shell, SQL, URL, provider routing, or credential path. Adversarial tests place P08 TOOL and P13 AGENT steps behind an `ALL` frontier containing one completed and one skipped predecessor and prove zero tool dispatch records, zero external HTTP effects, and zero agent-provider calls.

## Certification matrix

Tests cover `ALL` with all completed, completed plus skipped, all skipped, and unresolved predecessors; `ANY` with completed plus skipped, all skipped, all completed, and unresolved predecessors; invalid modes and meaningless `ANY`; API round trips; immutable version persistence; simple and multi-level condition joins; mandatory conjunctions; deep and nested branch propagation; skipped TOOL/AGENT effects; replay; two-worker condition completion; cancellation; and terminal absorption.

## Boundaries

Corrective 02 changes no claim, retry, scheduling, or crash-recovery policy. Automatic orphan reaping, stale-lease reassignment, ambiguous-effect reconciliation, failover, and redispatch remain NXS-P25. P15 scheduling and deployment remain untouched.
