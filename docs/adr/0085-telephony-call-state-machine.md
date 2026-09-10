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
* **Lower rank** than the current live state → `IGNORED` as stale / reordered. State is
  never rolled backward.
* **Equal rank, different state** → keep the earlier-timestamped / lower-sequence fact;
  ties keep the current state.
* **Higher rank** → `APPLIED`. A terminal is always reachable from a live state; a
  higher-rank live state is a legal forward move and any reordered / dropped
  intermediate is simply inferred (`ANSWERED` arriving before a delayed `RINGING` is
  accepted, and the late `RINGING` is then ignored).

The lifecycle is linear, so a provider STATE event has no "illegal live transition" —
the only `NXS_TELEPHONY_INVALID_STATE` is raised by the API surface (`send_dtmf` on a
call that is not `ANSWERED` / `BRIDGED`).

**Precedence for ordering.** `provider_sequence` (when the provider supplies one) wins;
otherwise `provider_timestamp`; otherwise the local monotonic rank rule. Both are
persisted on the call so a later event is compared against the authoritative recorded
fact.

**Concurrency.** Proven against real PostgreSQL: two duplicate provider events →
exactly one persisted transition + one `telephony_call_events` row; `ANSWERED` and
`COMPLETED` arriving concurrently → deterministic `COMPLETED` (terminal wins regardless
of interleaving); two concurrent hangups → idempotent `ENDING`; a delayed `RINGING`
racing a committed `COMPLETED` → stays `COMPLETED`; the same `provider_call_id` seen by
two Organizations → each has its own row (RLS + the `(organization_id, account_id,
provider_call_id)` unique).

**Disposition** is set on the terminal transition (`COMPLETED`→`ANSWERED`,
`BUSY`→`BUSY`, …) and never changed afterwards. `ringing_at` / `answered_at` /
`ended_at` are stamped once, on first entry to the corresponding state.

Consequences: telephony providers may duplicate, reorder or replay callbacks freely; the
persisted call state is a deterministic function of the multiset of events seen, and it
only ever moves forward.
