# NXS-P17 corrective #1: execution contract, ownership and first response

Starting reviewed commit: `0dea18309d07f81b58a111e132f86a153b91cdf5`, PR #38.

## Audit findings and fail-first evidence

The PostgreSQL regression selection initially produced six failures and three
passes: the first successful reply left `first_response_at` null, the handoff
lacked a contract version, a mismatched P13 contract was accepted, and three
invalid conversation authority shapes were accepted. Failed/ambiguous message
results and conflicting execution binding already failed closed.

## P13 binding

P13 exposes the certified in-process session boundary identity
`NXS-P13.agent-session.v1` as `AgentSession.CONTRACT_VERSION`. This identifies
the execution contract, not a provider/model version or mutable agent settings.
P17 captures it in `human_handoffs.p13_contract_version` when it commits
`AI_RETURN_PENDING`. Subsequent operations never change the captured value.
The database requires human-to-AI handoffs to carry both the expected contract
and the stable P13 key.

Before granting AI ownership, P17 verifies Organization, conversation, agent,
returned idempotency key, expected/returned contract version, exact session
binding and pending ownership generation. The resulting AI generation is the
pending generation plus one. Exact accepted replay returns current matching
authority; conflicting session identity or stale ownership fails closed.
An unexpected returned contract leaves ownership UNASSIGNED and records
ambiguous boundary evidence for P25; it never restores human authority or
creates a replacement identity.

## Database authority shape

`ck_conversation_ownership_authority_shape` enforces:

- AI: session present; human assignment and user absent.
- HUMAN: assignment and user present; AI session absent.
- UNASSIGNED: all three authority identifiers absent.

`work_item_id` remains the durable work reference. Claim retains its assignment
on the work item while ownership remains UNASSIGNED. Acceptance installs the
assignment/user together with HUMAN mode. Supervisor transfer validates either
an unaccepted claim or the exact current human owner before issuing fresh
authority. No check constraint is weakened to accommodate an intermediate row.

The unmerged P17 migration `f17a0b1c2d3e` includes the new column and constraints;
SQLAlchemy metadata has the same definitions. Clean upgrade and downgrade to
`b0c1d2e3f4a5` followed by upgrade were exercised.

## First response SLA

Successful P09 acceptance is persisted by P17 as CONSUMED with its message ID.
That same tenant transaction locks queue, work item, then authorization and
sets a null `first_response_at` from PostgreSQL `clock_timestamp()`. Later
messages and replay preserve the original timestamp. FAILED and AMBIGUOUS
results never advance it. If the final transaction fails, both the consumed
result and timestamp roll back together; ambiguous-effect recovery remains P25.

## Validation and scope

The focused P17 selection passed 48 tests. The full local suite passed 1,864
tests with coverage above the unchanged 90% threshold. Machine-readable
corrective evidence records the final validation and implementation reference.
RLS, tenant foreign keys, P09 provider authority and P25 recovery exclusions
remain unchanged. P18 and deployment are outside this corrective.
