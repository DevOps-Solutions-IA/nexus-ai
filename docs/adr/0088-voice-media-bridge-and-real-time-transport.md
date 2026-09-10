# ADR-0088: P11 MediaSession ↔ P12 voice streaming — the media bridge and the bounded transport

Status: Accepted; amended 2026-09-10 by the NXS-P12 independent-audit corrective
(provider signed-URL validation + connection host/port pinning against redirect-based
SSRF). Part of NXS-P12 (`NXS-VOICE-001`).

## The media bridge boundary

NXS-P11 ADR-0086 reserves the seam: "P12 attaches an external voice stream to an ACTIVE
media session's Asterisk `bridge_id` (`externalMedia`)". P12 realises this **without ever
exposing an arbitrary ARI URL, SIP URI, dialplan, AMI action, hostname, `externalMedia`
destination or UDP target**.

`nexus_ai.voice.bridge.plan_media_bridge` produces a `MediaBridgePlan` **entirely from
validated inputs**:

* `bridge_id` — from the trusted `telephony_media_sessions` row, re-validated against
  `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$` (rejects whitespace, `;`, CRLF, shell/SIP
  injection);
* `stream_ref` — an internal `vs-<24 hex>` identifier P12 generates; never provider- or
  caller-supplied;
* `gateway_host` / `gateway_port` — from the voice provider account's bounded,
  allow-listed configuration (`media_gateway_host` / `media_gateway_port`), **SSRF-guarded
  at config time and again at session start**: a host that is loopback, link-local, a
  cloud-metadata address (`169.254.169.254`, `100.100.100.200`), multicast, unspecified,
  reserved, or any non-private / public unicast target is **refused**; the port must be
  `1024..65535`;
* `audio_format` — the negotiated `AudioFormat`.

The plan is a **description an operations-owned media gateway executes**; P12 itself
opens no Asterisk connection and runs no shell. In this phase the production
`GatewayMediaChannel` performs no live RTP I/O — the ops gateway owns the socket — so a
voice session started against a real bridge completes deterministically instead of
hanging. Wiring the gateway transport is a documented follow-up and changes no P12
contract. Tests use an in-memory `LoopbackMediaChannel`.

## The bounded real-time transport

Every real-time path in P12 is bounded and cancellation-safe:

| Bound | Setting | Enforced by |
|---|---|---|
| provider REST timeout | `voice.provider_timeout_seconds` (15s) | `asyncio.wait_for` around `open_session` |
| WebSocket open / handshake | `voice.connect_timeout_seconds` (10s) | `WebsocketVoiceStreamTransport(open_timeout=…)` |
| provider idle (no frame) | `voice.idle_timeout_seconds` (30s) | `transport.recv(timeout=…)` → `NXS_VOICE_PROVIDER_TIMEOUT` |
| absolute session lifetime | `voice.max_session_seconds` (1h) | `asyncio.timeout` around the reader/writer wait |
| single provider message | `voice.max_message_bytes` (128 KiB) | reader check + `websockets` `max_size` |
| single audio frame | `voice.max_audio_frame_bytes` (32 KiB) | `BoundedFrameQueue` + reader/writer checks |
| audio queue depth | `voice.audio_queue_depth` (64) | `BoundedFrameQueue` — drop-oldest, never grows |

`BoundedFrameQueue` applies **backpressure by dropping the oldest frame** (recording the
drop count) rather than blocking a producer or growing without limit. There is no
unbounded `asyncio.Queue`, no blocking network I/O on an async path, and no transcript /
audio accumulation.

`WebsocketVoiceStreamTransport` wraps the already-vendored `websockets` asyncio client
with every knob bounded (`open_timeout`, `close_timeout`, `max_size`, `ping_interval`,
`ping_timeout`); every library exception is normalized to `NXS_VOICE_CONNECTION_FAILED`
or `StreamClosed`. `FakeVoiceStreamTransport` scripts frames and can inject a peer close,
an idle timeout or a malformed frame at a chosen point.

**Provider-URL trust (amended 2026-09-10, NXS-P12 corrective).** A provider-supplied
signed WebSocket URL is never opened on the strength of its `wss://` scheme alone. The
adapter runs `validate_provider_ws_url` first (scheme, no userinfo, host on the provider
allow-list, no IP literal, no loopback / RFC1918 / link-local / multicast / metadata
address, no unsafe port) and passes the validated `(host, port)` to
`connect(..., pin_host=, pin_port=)`. The transport then sets an explicit `host` / `port`
on the `websockets` client, which makes the library **refuse any cross-origin redirect**;
`WEBSOCKETS_MAX_REDIRECTS` bounds same-origin redirects. A caller cannot select the
provider REST origin either — it is `settings.voice.elevenlabs_api_base`, carried on
`VoiceSessionSpec.provider_api_base`, never a request field. Test/fake endpoints are
reached only through dependency injection (`transport_factory`, the `fake` provider),
never a public request option.

## The session runtime loop

`VoiceSessionRuntime.run` drives one duplex loop: a **reader** task (provider frame →
`parse_frame` → media write for audio, `on_event` for transcript / interruption /
session-started, `keepalive_reply` for a ping) and a **writer** task (media inbound audio
→ `serialize_audio` → provider send). On **any** exit — normal, peer close, protocol
error, idle timeout, or the outer task's cancellation — both workers are cancelled and
awaited, the transport is closed and the media channel is closed. Real-PostgreSQL and
unit tests prove repeated start / stop / disconnect cycles leak no `asyncio` task,
socket or DB session.

**Interruption / barge-in.** A provider `interruption` frame bumps a discard epoch,
clears the outbound audio queue, and emits `voice.interruption.started` /
`.completed`. Audio produced before the interruption is dropped and can never be
resurrected by a late provider frame.

**Latency.** Bounded provider-neutral metrics only — `connect_ms`,
`time_to_first_audio_ms`, `session_duration_ms` — recorded on the session and in
`voice.usage.recorded`. No secret, transcript content or high-cardinality label.
