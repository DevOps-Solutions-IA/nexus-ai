# ADR-0087: NXS voice provider abstraction + the ElevenLabs boundary

Status: Accepted. Establishes NXS-P12 (`NXS-VOICE-001`, `NXS-EL-001`): the permanent,
provider-neutral real-time voice layer:

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

**ElevenLabs boundary — official contract used (CONTRACT-CERTIFIED).** The adapter speaks
the ElevenLabs Conversational AI real-time API. Every contract element below is
**documented and treated as an ASSUMPTION** to reconcile against the live docs
(https://elevenlabs.io/docs); if the real contract differs, **only
`providers/elevenlabs.py` changes** — never the Nexus architecture or a security control:

* REST auth: `xi-api-key: <api key>` header on `{api_base}/v1/...`.
* Signed WebSocket URL: `GET {api_base}/v1/convai/conversation/get-signed-url?agent_id=<id>`
  returns `{"signed_url": "wss://..."}`. The signed URL carries the auth token in its
  query string; it is opened by the transport and **never logged, evented or persisted**
  (`redact_ws_url`).
* WebSocket JSON protocol: client → `conversation_initiation_client_data`, then
  `{"user_audio_chunk": "<base64 pcm>"}`; client → `{"type":"pong","event_id":n}` on a
  `ping`. Server → `conversation_initiation_metadata` (`conversation_id`), `audio`
  (`audio_base_64`), `user_transcript`, `agent_response`, `interruption`, `ping`.
* Audio: 16-bit PCM mono / G.711 `ulaw_8000`, chosen by agent config; the adapter carries
  the profile's negotiated `AudioFormat` and does not transcode.
* Post-call webhook: `ElevenLabs-Signature: t=<unix>,v0=<hex hmac_sha256("<t>." + body)>`
  with a shared webhook secret; timestamp freshness enforced.

Because no ElevenLabs credential or live account was available in this phase, the
integration is **CONTRACT-CERTIFIED** (fake provider + canned-frame adapter unit tests +
full lifecycle on real PostgreSQL), **not LIVE-PROVIDER-CERTIFIED**. Enabling a real key
requires no code change beyond storing the credential in the vault.

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
