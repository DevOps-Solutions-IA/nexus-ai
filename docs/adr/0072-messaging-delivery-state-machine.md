# ADR-0072: Messaging delivery state machine

Status: Accepted. Context: NXS-P09 tracks delivery lifecycle across three channels whose providers emit different, sometimes out-of-order, sometimes duplicate status callbacks. There must be one canonical definition of a legal transition.

Decision:

**One canonical status set** (`MessageStatus`): `RECEIVED`, `QUEUED`, `SENDING`, `SENT`, `DELIVERED`, `READ`, `FAILED`. `RECEIVED` is inbound-only; `QUEUED`/`SENDING`/`SENT`/`DELIVERED`/`READ` is the outbound forward path; `FAILED` is reachable from any non-terminal state. `READ` and `FAILED` are terminal.

**`nexus_ai.messaging.delivery` owns the rules.** A channel adapter maps a provider status string to a canonical `MessageStatus`; `apply_callback(current, reported)` folds it in:
- re-applying the current state → idempotent no-op (`duplicate=True`);
- a state strictly ahead on the rank order, or `FAILED` from a non-terminal state → applied (`advanced=True`), and the message's `sent_at` / `delivered_at` / `read_at` / `failed_at` + `error_code` are set;
- a state that would *regress* the canonical state (a late `sent` after `delivered`, a `READ → SENT`) → **safely ignored**, never applied;
- any callback on a terminal state → ignored.

A provider callback **never raises** — it reports whether it advanced. Only an explicit API-caller-requested transition raises (`require_transition` → `NXS_MSG_STATE_CONFLICT`).

**Event emission.** A callback that advances the state emits the matching `messaging.message.{sent,delivered,read,failed}` event in the same transaction as the message update; a duplicate or regressive callback emits nothing.

**Proven** (`tests/unit/test_messaging_primitives.py`, `tests/concurrency/test_messaging_concurrency.py`): monotonic advance, regressive-ignored, duplicate-idempotent, terminal-sticky, and concurrent unordered callbacks converging on a valid canonical state without regression.

Consequences: an operator sees one lifecycle regardless of provider vocabulary; a chaotic or replayed provider webhook stream cannot corrupt the message state; terminal-failure semantics are documented and enforced.
