# ADR-0087: NXS voice provider abstraction + the ElevenLabs boundary

Status: Accepted; amended 2026-09-10 by the NXS-P12 independent-audit corrective
(WebSocket SSRF / provider-URL trust hardening, truthful `AI → PENDING_HUMAN → HUMAN`
handoff lifecycle, and a dated ElevenLabs protocol-reconciliation record). Establishes
NXS-P12 (`NXS-VOICE-001`, `NXS-EL-001`): the permanent, provider-neutral real-time voice
layer:

    PSTN / SIP -> Asterisk 22 LTS / ARI -> NXS-P11 Telephony Foundation
        -> Call + ACTIVE MediaSession -> NXS-P12 Voice Gateway -> ElevenLabs
        -> (future) NXS-P13 AI Agent Runtime

**Scope.** P12 owns VOICE PROVIDER INTEGRATION only: the provider adapter contract, the
ElevenLabs Conversational AI real-time integration, the voice-session lifecycle attached
to an ACTIVE P11 media session, the bounded audio transport, interruption semantics, the
media-bridge boundary, and controlled AI↔human handoff (control-plane only). P12 ships
NO autonomous reasoning, NO tool execution, NO workflows / campaigns / scheduler, NO
call recording. NXS-P11 stays authoritative for call state, ownership, phone numbers,
SIP routing, hangup, DTMF and Asterisk call control — P12 never creates an alternative
source of truth for a call.

**Package layout.** `nexus_ai.voice` — `entities` (domain values + strict API
contracts), `errors` (`NXS_VOICE_*`), `state_machine` (fold rules, ADR-0089), `audio`
(fail-closed codec/rate governance), `idempotency` (session fingerprint), `events` (P04
payloads, NXS-EVENT-012), `redaction`, `bridge` (ADR-0088), `transport` (bounded
real-time primitives), `runtime` (the streaming loop), `media`, `providers/{base,
fake,elevenlabs,registry}`, `service` (`VoiceService`), `webhooks`
(`InboundVoiceService`). Persistence is `nexus_ai.domain.voice` — 6 tenant-owned tables.

**Provider abstraction.** `VoiceProviderAdapter` is a *normalizer*: `open_session`
(REST leg — obtains how to open the real-time transport), `serialize_init` /
`serialize_audio` / `keepalive_reply`, `parse_frame` (provider WebSocket frame →
`VoiceProviderEvent`), `verify_webhook` / `parse_webhook`. A `fake` adapter is a
first-class provider. The rest of Nexus imports **no ElevenLabs type**; a future
provider implements the same contract. Every provider REST call goes through an injected
`VoiceHttpTransport` — in production `GovernedVoiceHttpTransport` over the NXS-P07
`GovernedHttpExecutor` (SSRF-safe, TLS-verified, bounded). Every WebSocket goes through a
`VoiceStreamTransport` (ADR-0088).

**ElevenLabs boundary — contract-reconciliation record (reconciled 2026-09-10).** The
adapter speaks the ElevenLabs Conversational AI real-time API, reconciled on 2026-09-10
against the official Conversational AI documentation
(https://elevenlabs.io/docs/conversational-ai). This is a dated record of the exact
supported protocol subset, not a list of open assumptions. If the live contract later
differs, **only `providers/elevenlabs.py` changes** — never the Nexus architecture or a
security control.

* REST auth: `xi-api-key: <api key>` header on `{provider_api_base}/v1/...`. The origin
  is **trusted server configuration** (`settings.voice.elevenlabs_api_base`, a bare
  `https://` origin) carried on `VoiceSessionSpec.provider_api_base` — **never a caller**.
  `StartVoiceSessionRequest` has no endpoint field and its `options` map is a fixed
  allow-list (`ALLOWED_SESSION_OPTION_KEYS`) that cannot name `api_base` / `url` /
  `endpoint` / `host` / `ws_url` / `signed_url` / `api_key`.
* Signed WebSocket URL: `GET {api_base}/v1/convai/conversation/get-signed-url?agent_id=<id>`
  returns `{"signed_url": "wss://api.elevenlabs.io/..."}`. **Every signed destination is
  validated before the transport is touched** (`validate_provider_ws_url`): scheme must be
  `wss://`; no userinfo; the host must be on `_ELEVENLABS_WS_HOSTS`
  (`{"api.elevenlabs.io"}` today — a regional endpoint is added *here*, never via a caller
  or a provider response); an IP literal (loopback, RFC1918, CGNAT, link-local, multicast,
  `169.254.169.254`) is refused outright; unsafe ports (22/23/25/3306/5432/6379/…) are
  refused. The validated `(host, port)` is **pinned** on the `WebsocketVoiceStreamTransport`
  connection so a cross-origin redirect cannot escape (`websockets` refuses a cross-origin
  redirect when an explicit host/port is set; `WEBSOCKETS_MAX_REDIRECTS` bounds the rest).
  The signed URL carries the auth token in its query string; it is opened by the transport
  and **never logged, evented or persisted** (`redact_ws_url`).
* WebSocket JSON protocol — the **supported subset**: client → `conversation_initiation_client_data`,
  then `{"user_audio_chunk": "<base64 pcm>"}`; client → `{"type":"pong","event_id":n}` on
  a `ping`. Server frames NORMALIZED: `conversation_initiation_metadata` (`conversation_id`)
  → SESSION_STARTED; `audio` (`audio_event.audio_base_64`) → AUDIO_OUTPUT; `user_transcript`
  → TRANSCRIPT; `agent_response` → AGENT_TEXT; `agent_response_correction`
  (`corrected_agent_response`) → AGENT_TEXT (a transport-level fact — P12 does not reason
  over it); `interruption` → INTERRUPTION; `ping` → KEEPALIVE; `error` → ERROR.
* WebSocket frames DOCUMENTED-BUT-OUT-OF-SCOPE — safely dropped (`_IGNORED_FRAMES`), never
  treated as malformed protocol, never acted on: `client_tool_call` / `client_tool_result`
  (**P12 does NOT execute client tools — that is NXS-P13 / the Tool Engine**),
  `mcp_connection_status` / `mcp_tool_call` / `mcp_tool_result`,
  `internal_tentative_agent_response`, `vad_score`, `asr_initiation_metadata`,
  `contextual_update`. A frame on neither list → `NXS_VOICE_PROTOCOL_ERROR` (fail closed).
* Audio: 16-bit PCM mono / G.711 `ulaw_8000`, chosen by agent config; the adapter carries
  the profile's negotiated `AudioFormat` and does not transcode.
* Post-call webhook: `ElevenLabs-Signature: t=<unix>,v0=<hex hmac_sha256("<t>." + body)>`
  with a shared webhook secret; timestamp freshness enforced.

Because no ElevenLabs credential or live account was available in this phase, the
integration is **CONTRACT-CERTIFIED** (fake provider + canned-frame adapter unit tests +
SSRF matrix + full lifecycle on real PostgreSQL), **not LIVE-PROVIDER-CERTIFIED**.
Enabling a real key requires no code change beyond storing the credential in the vault.

**Controlled AI↔human handoff — truthful lifecycle.** `handoff_state` is a three-state
machine: `AI → PENDING_HUMAN → HUMAN`. `request_handoff` atomically moves `AI →
PENDING_HUMAN`, emits `voice.handoff.requested`, and detaches / cancels the AI voice
stream — it does **NOT** emit `voice.handoff.completed` and does **NOT** claim `HUMAN`,
because P12 neither performs nor verifies the actual human bridge. Only `confirm_handoff`
— the tenant-scoped seam a future NXS-P17 bridge-completion callback invokes with an
authoritative `bridge_reference` — moves `PENDING_HUMAN → HUMAN` and emits
`voice.handoff.completed`. `request_handoff` is idempotent (a second call is a no-op, no
duplicate event), rejects a terminal session for the initial transition, and is
deterministic under concurrent callers (row `FOR UPDATE`; exactly one
`voice.handoff.requested`). `confirm_handoff` refuses any state other than
`PENDING_HUMAN` and cannot cross tenants.

**Voice profile / voice-id governance.** A caller never supplies a raw `voice_id` /
`agent_id` / `model` — those are an Organization-owned `voice_profiles` row referenced by
`voice_profile_id`, validated for tenant + account ownership + ACTIVE status. Provider
identifiers are bounded, opaque and isolated on that row.

**Credential security.** The ElevenLabs API key and webhook secret live in the NXS-P07
encrypted vault (`voice_secrets`, Fernet ciphertext). They are fetched only at the
provider boundary, only when required. `SecretMaterial` redacts itself and is never a
serialisable model. Contract + security tests prove no key, signed URL, Authorization
header, raw frame or audio reaches a table, a P04 event, an API response, an exception or
a log line. The LLM never receives a provider secret (there is no LLM in P12).

**Bounded account configuration.** `account_config.validate_voice_account_configuration`:
an allow-list per provider (`media_gateway_host` / `media_gateway_port` / `agent_prefix`
/ `region`), typed bounds, and the media-gateway pair fully SSRF-validated at config time
(ADR-0088). No per-session URL / header / command surface.

**RBAC.** `voice:read` / `voice:use` / `voice:configure`, deterministic ids
`b2000000-…-00000000002a..2c`, seeded as a migration delta: owner + admin get all three;
`org_member` gets `read` + `use`. `ROLE_PERMISSIONS` (frozen at P03) is unchanged.

**Dependency.** `websockets==17.1` — already vendored transitively via `uvicorn[standard]`
and covered by the existing `pip-audit` gate — is promoted to a pinned direct dependency
for the `WebsocketVoiceStreamTransport`. No new package enters the lock.

Consequences: NXS-P13 attaches an agent runtime to the frozen voice-session + transcript
event contract without redesigning P12; a provider swap is an adapter, not a rewrite.
