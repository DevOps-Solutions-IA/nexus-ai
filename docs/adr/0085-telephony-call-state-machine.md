# ADR-0085: Telephony call state machine — monotonic, out-of-order-safe, fail-closed

Status: Accepted. Part of NXS-P11 (`NXS-TEL-001`).

**States.** `CREATED → RINGING → EARLY_MEDIA → ANSWERED → BRIDGED → ENDING → COMPLETED`,
with terminal alternatives `FAILED` / `CANCELLED` / `BUSY` / `NO_ANSWER`. Every state has
a **rank** (`CREATED`=0 … `ENDING`=5, every terminal = 6). `telephony_calls.state` and
`state_rank` are `CHECK`-constrained.

**Folding a provider event** (`state_machine.fold_state`, applied inside the inbound
webhook's per-call `SELECT … FOR UPDATE` transaction):

* **Already terminal** → `IGNORED`. A delayed `RINGING` after `COMPLETED` is a no-op; a
  second `COMPLETED` is a no-op; a *different* terminal never overwrites the first.
* **Same state** → `IGNORED` (idempotent).
* **Provably-earlier provider order** → `IGNORED` (`stale-provider-order`). A
  *non-terminal* event the provider itself orders before our recorded fact — a strictly
  lower `provider_sequence` when both events carry one, otherwise a strictly older
  `provider_timestamp` when both carry one — is a reordered stale callback and is
  ignored **even if its rank is higher**: we already hold newer information. A terminal
  outcome is exempt and still always wins.
* **Lower rank** than the current live state → `IGNORED` as stale / reordered. State is
  never rolled backward.
* **Equal rank, different state** → unreachable for live states (rank is a bijection
  with the live state); a defensive idempotent no-op.
* **Higher rank** → `APPLIED`. A terminal is always reachable from a live state; a
  higher-rank live state is a legal forward move and any reordered / dropped
  intermediate is simply inferred (`ANSWERED` arriving before a delayed `RINGING` is
  accepted, and the late `RINGING` is then ignored).

The lifecycle is linear, so a provider STATE event has no "illegal live transition" —
the only `NXS_TELEPHONY_INVALID_STATE` is raised by the API surface (`send_dtmf` on a
call that is not `ANSWERED` / `BRIDGED`).

**Precedence for ordering (enforced).** `provider_sequence` (when both the recorded and
the proposed event carry one) is authoritative; failing that, `provider_timestamp` (when
both carry one); failing that, monotonic rank alone. `provider_sequence` and
`provider_timestamp` are persisted on the call (and re-hydrated into the domain object)
so every incoming event is compared against the authoritative recorded fact — a
higher-rank event with a strictly lower sequence does **not** advance the call
(`fold_state` regression + a real-PostgreSQL regression prove it).

**Concurrency.** Proven against real PostgreSQL: two duplicate provider events →
exactly one persisted transition + one `telephony_call_events` row; `ANSWERED` and
`COMPLETED` arriving concurrently → deterministic `COMPLETED` (terminal wins regardless
of interleaving); two concurrent hangups → idempotent `ENDING`; a delayed `RINGING`
racing a committed `COMPLETED` → stays `COMPLETED`; a higher-rank event carrying a
strictly lower `provider_sequence` than the recorded fact → ignored, the call does not
advance; the same `provider_call_id` seen by two Organizations → each has its own row
(RLS + the `(organization_id, account_id, provider_call_id)` unique).

**Disposition** is set on the terminal transition (`COMPLETED`→`ANSWERED`,
`BUSY`→`BUSY`, …) and never changed afterwards. `ringing_at` / `answered_at` /
`ended_at` are stamped once, on first entry to the corresponding state.

Consequences: telephony providers may duplicate, reorder or replay callbacks freely; the
persisted call state is a deterministic function of the multiset of events seen, and it
only ever moves forward.
