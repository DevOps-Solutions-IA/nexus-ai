# ADR-0089: Voice-session lifecycle, idempotency, failure semantics and the AI↔human handoff

Status: Accepted. Part of NXS-P12 (`NXS-VOICE-001`, `NXS-EL-001`).

## Lifecycle

`PENDING → CONNECTING → CONNECTED → STREAMING → ENDING`, with terminal alternatives
`COMPLETED` / `FAILED` / `CANCELLED`. Every state has a **rank** (`PENDING`=0 …
`ENDING`=4, every terminal = 5). `voice_sessions.state` and `state_rank` are
`CHECK`-constrained.

`state_machine.fold_session_state` is the **same design as the NXS-P11 telephony call
state machine (ADR-0085)** — deterministic, monotonic, fail-closed:

* already terminal → `IGNORED` (a delayed `CONNECTED` after `COMPLETED` is a no-op; a
  different terminal never overwrites the first);
* same state → `IGNORED`;
* a **non-terminal** event the provider orders strictly before the recorded fact (lower
  `provider_sequence`, else older `provider_timestamp`) → `IGNORED` (`stale-provider-order`)
  even at higher rank — a terminal is exempt and always wins;
* lower rank → `IGNORED` (stale / reordered);
* higher rank → `APPLIED` (forward move; a reordered / dropped intermediate is inferred).

The live lifecycle is linear, so rank is a bijection with the live state and there is no
"illegal live transition". `NXS_VOICE_INVALID_STATE` is raised only by the API surface
(`request_handoff` on an already-terminal session).

`start_session` is a **fast synchronous** operation: it validates, persists (idempotent),
transitions `PENDING → CONNECTING`, emits `voice.session.created` /
`voice.session.connecting`, and hands the long-lived streaming loop to a background
runtime task the service tracks. The runtime's `on_connected` hook transitions to
`CONNECTED`; the first provider `SESSION_STARTED` transitions to `STREAMING`; the runtime
outcome drives the terminal transition, records `voice_usage_records` and emits
`voice.session.{completed,failed,cancelled}` + `voice.usage.recorded` — all atomically in
one tenant transaction. On shutdown the service cancels every in-flight runtime task.

## Idempotency

`voice_sessions.idempotency_key` is unique per `organization_id`. Two `start_session`
requests under one key are the SAME logical session only when a canonical **request
fingerprint** matches — a SHA-256 over the semantic fields:

    media_session_id · provider_account_id · voice_profile_id · normalized options

`organization_id` is implicit (tenant scope + the per-organization unique key).
`correlation_id` is **observational** and excluded. The fingerprint is persisted on
`voice_sessions.request_fingerprint`; **all four** idempotency paths compare exactly that
one value (top-level `_replay`, top-of-transaction `by_idempotency_key`, `IntegrityError`
winner resolution, bounded-retry lookup).

* same key + identical fingerprint → the winner's session is replayed; a concurrent
  identical start is exactly one logical session and one provider-side creation (proven
  under an 8-way real-PostgreSQL race);
* same key + any semantic difference → deterministic `NXS_VOICE_IDEMPOTENCY_CONFLICT`.

**One live session per media session.** A partial unique index
`uq_voice_sessions_one_live_per_media` on `(organization_id, media_session_id) WHERE
state IN (live states)` guarantees at most one non-terminal voice session per P11 media
session. A concurrent second start (different key) → `NXS_VOICE_MEDIA_NOT_READY` (proven
under a 4-way race → one winner, three conflicts, one row).

**Ambiguous provider creation.** The provider REST leg (`open_session`) is bounded by
`voice.provider_timeout_seconds`; on timeout the runtime does NOT retry or create a
second external session — the session records `error_code = NXS_VOICE_PROVIDER_TIMEOUT`
and ends. A retry under the same key replays the recorded session.

## Failure semantics

| Case | Behaviour |
|---|---|
| provider REST unavailable / 4xx-5xx before connect | session → `FAILED`, `NXS_VOICE_CONNECTION_FAILED` / `NXS_VOICE_PROVIDER_ERROR` / `NXS_VOICE_NOT_AUTHORIZED` |
| provider REST timeout | session ends `NXS_VOICE_PROVIDER_TIMEOUT`; a same-key retry replays — never a second external session |
| provider disconnect after connect | reader/writer stop, transport + media closed, session → `COMPLETED` or `FAILED`, usage + latency recorded |
| malformed / oversized provider frame | session → `FAILED`, `NXS_VOICE_PROTOCOL_ERROR` |
| provider idle (no frame within the window) | session ends, `NXS_VOICE_PROVIDER_TIMEOUT` |
| media session not `ACTIVE` / not owned | `NXS_VOICE_MEDIA_NOT_READY` — a stream is never attached |
| DB / P04 outbox failure mid-transition | the whole tenant transaction rolls back — no partial state, no orphan event |
| duplicate signed post-call webhook | first `reconciled`, every replay `replayed` (durable per `(org, account, provider_event_id)`) |
| stale / unsigned / wrong-secret webhook | `NXS_VOICE_WEBHOOK_REPLAY` / `NXS_VOICE_WEBHOOK_INVALID` — never ACKed as accepted |
| process restart between response and persistence | `start_session` commits `PENDING`/`CONNECTING` before the runtime task; a restart leaves a non-terminal session that a stop or the session lifetime ceiling resolves |

## AI↔human handoff (`NXS-VOICE-001`)

`request_handoff` is **control-plane only**: it transitions `handoff_state`
`AI → HUMAN`, emits `voice.handoff.requested` / `voice.handoff.completed`, and detaches
the AI voice stream (cancels the runtime task). **NXS-P11 owns the actual call bridge**
to the human agent; P12 records the intent and stops producing AI audio. P12 decides
nothing about *what* the AI says or does — that is NXS-P13.

## Persistence

Six tenant-owned tables, forced RLS, `nexus_runtime` non-bypass, tenant schema guard
clean, migration `a1b2c3d4e5f6` (revises `f4a5b6c7d8e9`), fully reversible:

| Table | Notes |
|---|---|
| `voice_provider_accounts` | `(provider, external_account_id)` + `webhook_token` GLOBALLY unique |
| `voice_secrets` | Fernet ciphertext only |
| `voice_profiles` | Organization-owned voice config; composite FK → account (RESTRICT) |
| `voice_sessions` | composite tenant-aware FKs → `voice_provider_accounts` (RESTRICT), `voice_profiles` (`SET NULL (voice_profile_id)`), **`telephony_calls`** (RESTRICT), **`telephony_media_sessions`** (RESTRICT); `UNIQUE(org, id)`, `UNIQUE(org, account_id, provider_session_id)`, `UNIQUE(org, idempotency_key)`, partial unique one-live-per-media |
| `voice_provider_events` | durable per-provider-event idempotency + audit; `UNIQUE(org, account_id, provider_event_id)` |
| `voice_usage_records` | bounded numeric usage / latency per session; composite FK → session CASCADE. **No audio, no transcript body** |

**Events (P04 outbox, NXS-EVENT-012).** `voice.session.{created,connecting,connected,
streaming,ending,completed,failed,cancelled}`, `voice.provider.{connected,disconnected}`,
`voice.transcript.{partial,final}` (a **char count only** — never the body),
`voice.interruption.{started,completed}`, `voice.handoff.{requested,completed}`,
`voice.usage.recorded`. Safe metadata only — no API key, signed URL, Authorization
header, raw frame, raw SDP or audio.

**No raw audio persistence.** P12 persists no call audio, in any table, log, event or
CI artifact. Recording is a separate compliance capability, out of P12 and P13 scope.
