# NXS-P13 Cancel-First Post-Merge Corrective

## Incident identity

- Canonical failing `main`: `269a74f9d851a16fc9a60040e324b6dc35a4bb1b`.
- Exact-main NXS CI run: `34712532243` (`FAILURE`); NXS Security succeeded.
- Failing test: `tests/concurrency/test_agent_tool_permit_claim_ownership.py::test_cancel_first_still_blocks_tool_permit_and_dispatch`.
- Exact-main suite: 1,552 passed, 1 failed, 91.85% coverage.
- Corrective implementation commit: `d2729f26220fb8d0391d145019003e5ba86a4748`.

## Fail-first evidence and classification

The exact-main CI failure is authoritative fail-first evidence: the test expected
`AgentCancelledError` or `asyncio.CancelledError`, but the submitted turn returned normally. The
unchanged test then failed before its direct permit assertion. Local canonical-main repetition
passed 30/30, proving the failure was intermittent rather than a deterministic production-code
failure.

Root cause classification: **C — test synchronization race**. No production fencing defect was
found.

The old `_model_turn_hang` helper treated a durable `execution_owner_id` as proof that the inner
`_run_turn` task was registered and sleeping in the fake provider. Those events are not equivalent:
`_open_turn` commits the owner first; `submit_turn` then resolves the agent and calculates its
deadline before creating and registering `_run_turn` in `_turn_tasks`. In that interval,
same-worker `cancel_session` finds no tracked task and waits for the session's process-local lock.
The turn can complete, release the lock, and return normally before cancellation commits. A
five-second `asyncio.sleep` made that window unlikely but did not prove ordering.

The fake provider does not absorb cancellation: its delay is a plain `await asyncio.sleep(...)`,
which propagates task cancellation. The failure therefore does not show swallowed cancellation;
it shows that the test had not proved the task had reached the cancellable provider call.

For the exact failing path, the normal task return proves the turn completed before the
same-worker cancellation acquired the session lock. The session subsequently became `CANCELLED`,
as the preceding assertion passed. The scripted model response contained no tool call, so the path
could not create a tool-dispatch permit, enter `AgentToolBridge`/P08, create a
`ToolExecutionRecord`, or reach the mock external server. The CI job did not persist its ephemeral
database, so no stronger database-count claim is made about that historical run.

## Preserved invariant and correction

The production invariant remains unchanged: when session cancellation commits before a new tool
permit authorization, PostgreSQL serialization makes the session terminal, the authorization
raises `ExecutionRevoked`, no permit is inserted, and `AgentRuntime` cannot enter
`AgentToolBridge.execute`.

The corrected test uses an instrumented model adapter with two `asyncio.Event` barriers:

1. Worker A durably claims a turn and commits one model-dispatch permit.
2. The adapter signals that Worker A is blocked before returning a tool request.
3. Independent Worker B, which cannot see or cancel Worker A's process-local task, durably commits
   session cancellation.
4. The test reads back `CANCELLED` from PostgreSQL and proves Worker A remains blocked.
5. The release event lets Worker A return the tool request.
6. Tool authorization observes the durable terminal session and raises `ExecutionRevoked`, mapped
   to `AgentCancelledError` for the submitted turn.

The final assertions prove: model permit count 1 (model behavior unchanged), tool permit count 0,
bridge/P08 entry count 0, `ToolExecutionRecord` count 0, external HTTP/business-effect count 0,
and a terminal `CANCELLED` turn with `NXS_AGENT_CANCELLED`. The test also directly retries tool
authorization and confirms it remains fenced.

## Validation

- Canonical-main reproduction before correction: 30 consecutive passes locally; exact-main CI
  supplied the intermittent fail-first execution.
- Corrected target stability: 50 consecutive passes.
- Corrective #10 and adjacent ownership/cancellation matrix: 46 passed.
- Complete P13 test surface: 185 passed.
- Full repository suite: 1,553 passed, 0 failed, 91.82% coverage.
- Formatting: 569 files formatted; check passed.
- Ruff: passed.
- Mypy: 260 source files; passed.
- NXS repository/schema validation: passed.
- Bandit high-severity gate: passed.
- `pip-audit`: no known vulnerabilities; the local unpublished `nexus-ai` package is not on PyPI.

## Scope and limitations

Changed implementation surface is test-only. Production P13 service, runtime, repository,
ToolBridge, migrations, state, and historical P13 evidence remain unchanged. No Workflow Engine or
other NXS-P14 implementation file was added or modified. No deployment occurred.

This corrective does not claim exactly-once physical effects after a permit has already committed.
The existing crash/ambiguous-effect recovery boundary remains assigned to NXS-P25. It proves the
narrower P13 guarantee: cancellation that durably wins before authorization prevents every new
tool permit and dispatch.
