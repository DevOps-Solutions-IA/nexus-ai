"""The ElevenLabs voice adapter (NXS-P12, ADR-0087).

Speaks the ElevenLabs Conversational AI real-time API behind the Nexus-neutral
:class:`VoiceProviderAdapter`. Nothing outside this module imports an ElevenLabs shape.

CONTRACT-RECONCILIATION RECORD — reconciled 2026-09-10 against the ElevenLabs
Conversational AI docs (https://elevenlabs.io/docs/conversational-ai). Still
**CONTRACT-CERTIFIED, NOT LIVE-PROVIDER-CERTIFIED** until a real API key is exercised.

REST
* Auth: ``xi-api-key: <api key>`` header on ``{provider_api_base}/v1/...``. The origin is
  trusted server configuration (``settings.voice.elevenlabs_api_base``) — NEVER a caller.
* Signed WebSocket URL: ``GET {api_base}/v1/convai/conversation/get-signed-url?agent_id=<id>``
  returns ``{"signed_url": "wss://api.elevenlabs.io/..."}``. Before the transport opens
  it, the host is checked against ``_ELEVENLABS_WS_HOSTS`` and the target is refused if it
  is an IP literal / loopback / private / metadata / unsafe-port host
  (:func:`validate_provider_ws_url`). The signed URL carries the auth token in its query
  string; it is opened by the transport and NEVER logged, evented or persisted.

WebSocket JSON frames (the supported subset):
* client -> ``conversation_initiation_client_data`` (first), ``{"user_audio_chunk":"<b64>"}``,
  ``{"type":"pong","event_id":<n>}`` in reply to a ``ping``.
* server, NORMALIZED:
    - ``conversation_initiation_metadata``     -> SESSION_STARTED (carries ``conversation_id``)
    - ``audio`` (``audio_event.audio_base_64``) -> AUDIO_OUTPUT
    - ``user_transcript``                       -> TRANSCRIPT (final)
    - ``agent_response``                        -> AGENT_TEXT (final)
    - ``agent_response_correction``             -> AGENT_TEXT (the corrected text; a
      transport-level fact — P12 does not reason over it)
    - ``interruption``                          -> INTERRUPTION
    - ``ping``                                  -> KEEPALIVE (a ``pong`` is sent)
    - ``error``                                 -> ERROR
* server, DOCUMENTED-BUT-OUT-OF-SCOPE (safely ignored, never malformed, never acted on):
    - ``client_tool_call`` / ``client_tool_result`` — P12 does **NOT** execute client
      tools; that is NXS-P13 / the Tool Engine. The frame is dropped.
    - ``mcp_connection_status`` / ``mcp_tool_call`` / ``mcp_tool_result``
    - ``internal_tentative_agent_response`` / ``vad_score`` / ``asr_initiation_metadata``
    - ``contextual_update`` (a client->server frame; ignored if echoed)
* A frame type not on either list -> ``NXS_VOICE_PROTOCOL_ERROR`` (fail closed).

Audio: 16-bit PCM mono (``pcm_16000`` / ``pcm_8000``) or G.711 ``ulaw_8000``, chosen by
the agent config; the adapter carries the profile's negotiated :class:`AudioFormat` and
does not transcode.

Post-call webhook: ``ElevenLabs-Signature: t=<unix>,v0=<hex hmac_sha256("<t>." + body)>``
with a shared webhook secret; timestamp freshness is enforced.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from nexus_ai.integrations.credentials import SecretMaterial
from nexus_ai.integrations.errors import IntegrationCredentialUnavailableError
from nexus_ai.voice.entities import TranscriptRole, VoiceProviderEvent, VoiceProviderEventKind
from nexus_ai.voice.errors import (
    VoiceConfigInvalidError,
    VoiceNotAuthorizedError,
    VoiceProtocolError,
    VoiceProviderError,
)
from nexus_ai.voice.providers.base import (
    HttpError,
    ProviderSessionInit,
    VoiceHttpTransport,
    VoiceSessionSpec,
    WebhookContext,
    WebhookParseResult,
    provider_rest_error,
    validate_provider_ws_url,
    verify_elevenlabs_style_signature,
)

#: The ElevenLabs real-time WebSocket host allow-list. A regional endpoint (if ElevenLabs
#: introduces one) is added here — never through a caller or a provider response.
_ELEVENLABS_WS_HOSTS: frozenset[str] = frozenset({"api.elevenlabs.io"})

#: Documented ElevenLabs server frames that P12 deliberately drops (never acts on, never
#: treats as a protocol error). ``client_tool_call`` is here because tool execution is
#: NXS-P13 / the Tool Engine, not P12.
_IGNORED_FRAMES: frozenset[str] = frozenset(
    {
        "client_tool_call",
        "client_tool_result",
        "mcp_connection_status",
        "mcp_tool_call",
        "mcp_tool_result",
        "internal_tentative_agent_response",
        "vad_score",
        "asr_initiation_metadata",
        "contextual_update",
    }
)


class ElevenLabsVoiceAdapter:
    key = "elevenlabs"

    async def open_session(
        self, spec: VoiceSessionSpec, secret: Any, http: VoiceHttpTransport
    ) -> ProviderSessionInit:
        api_key = _api_key(secret)
        api_base = (spec.provider_api_base or "").rstrip("/")
        if not api_base.startswith("https://"):
            raise VoiceConfigInvalidError(
                "the ElevenLabs API base is not configured as an https origin "
                "(it comes from server configuration, never a request)"
            )
        url = (
            f"{api_base}/v1/convai/conversation/get-signed-url"
            f"?agent_id={_qp(spec.provider_voice_ref)}"
        )
        try:
            response = await http.request(
                method="GET",
                url=url,
                headers={"xi-api-key": api_key, "Accept": "application/json"},
                body=None,
                timeout_seconds=None,
            )
        except HttpError as exc:
            if exc.timeout:
                raise provider_rest_error(
                    "ElevenLabs signed-url request timed out", status_code=504
                ) from exc
            raise provider_rest_error(
                "ElevenLabs signed-url request failed", status_code=502
            ) from exc
        if response.status_code in (401, 403):
            raise VoiceNotAuthorizedError("ElevenLabs rejected the API key")
        if response.status_code >= 400:
            raise provider_rest_error(
                "ElevenLabs signed-url request was rejected", status_code=response.status_code
            )
        try:
            body = json.loads(response.body or b"{}")
            signed_url = str(body["signed_url"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise VoiceProviderError("ElevenLabs returned no usable signed url") from exc
        # Every signed WebSocket destination is validated before the transport touches it.
        target = validate_provider_ws_url(signed_url, allowed_hosts=_ELEVENLABS_WS_HOSTS)
        return ProviderSessionInit(
            url=target.url,
            headers={},
            negotiated_format=spec.output_format,
            ws_host=target.host,
            ws_port=target.port,
            provider_session_id=None,  # arrives on conversation_initiation_metadata
        )

    def serialize_init(self, spec: VoiceSessionSpec) -> str:
        overrides: dict[str, Any] = {
            "agent": {"first_message": None},
            "conversation_config_override": {
                "agent": {"language": spec.options.get("language", "en")},
            },
        }
        return json.dumps(
            {
                "type": "conversation_initiation_client_data",
                "conversation_config_override": overrides["conversation_config_override"],
            }
        )

    def serialize_audio(self, pcm: bytes) -> str:
        return json.dumps({"user_audio_chunk": base64.b64encode(pcm).decode("ascii")})

    def keepalive_reply(self, sequence: int | None) -> str:
        message: dict[str, Any] = {"type": "pong"}
        if sequence is not None:
            message["event_id"] = sequence
        return json.dumps(message)

    def parse_frame(self, raw: bytes | str) -> VoiceProviderEvent | None:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise VoiceProtocolError("the ElevenLabs frame is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise VoiceProtocolError("the ElevenLabs frame must be a JSON object")

        frame_type = str(payload.get("type") or "")
        if frame_type == "ping":
            event = payload.get("ping_event") or {}
            return VoiceProviderEvent(
                kind=VoiceProviderEventKind.KEEPALIVE,
                provider_sequence=_seq(event.get("event_id")),
            )
        if frame_type == "conversation_initiation_metadata":
            meta = payload.get("conversation_initiation_metadata_event") or {}
            return VoiceProviderEvent(
                kind=VoiceProviderEventKind.SESSION_STARTED,
                provider_session_id=_opt_str(meta.get("conversation_id")),
            )
        if frame_type == "audio":
            event = payload.get("audio_event") or {}
            chunk = event.get("audio_base_64") or event.get("audio_base64")
            if not isinstance(chunk, str):
                raise VoiceProtocolError("the ElevenLabs audio frame has no payload")
            try:
                audio = base64.b64decode(chunk, validate=True)
            except (ValueError, TypeError) as exc:
                raise VoiceProtocolError("an ElevenLabs audio chunk is not valid base64") from exc
            return VoiceProviderEvent(
                kind=VoiceProviderEventKind.AUDIO_OUTPUT,
                audio=audio,
                provider_sequence=_seq(event.get("event_id")),
            )
        if frame_type == "user_transcript":
            event = payload.get("user_transcription_event") or {}
            return VoiceProviderEvent(
                kind=VoiceProviderEventKind.TRANSCRIPT,
                text=str(event.get("user_transcript") or "")[:8_192],
                role=TranscriptRole.USER,
                is_final=True,
            )
        if frame_type == "agent_response":
            event = payload.get("agent_response_event") or {}
            return VoiceProviderEvent(
                kind=VoiceProviderEventKind.AGENT_TEXT,
                text=str(event.get("agent_response") or "")[:8_192],
                role=TranscriptRole.AGENT,
                is_final=True,
            )
        if frame_type == "agent_response_correction":
            event = payload.get("agent_response_correction_event") or {}
            corrected = event.get("corrected_agent_response") or event.get("agent_response")
            return VoiceProviderEvent(
                kind=VoiceProviderEventKind.AGENT_TEXT,
                text=str(corrected or "")[:8_192],
                role=TranscriptRole.AGENT,
                is_final=True,
            )
        if frame_type == "interruption":
            return VoiceProviderEvent(kind=VoiceProviderEventKind.INTERRUPTION)
        if frame_type in _IGNORED_FRAMES:
            # A documented-but-out-of-scope frame (incl. client_tool_call — P12 NEVER
            # executes client tools; that is NXS-P13). Dropped, not a protocol error.
            return None
        if frame_type == "error":
            return VoiceProviderEvent(
                kind=VoiceProviderEventKind.ERROR,
                error_label=str(payload.get("message") or payload.get("reason") or "error")[:200],
            )
        raise VoiceProtocolError(f"unknown ElevenLabs frame type {frame_type!r}")

    def verify_webhook(self, ctx: WebhookContext, secret: Any, *, tolerance_seconds: int) -> None:
        headers = {k.lower(): v for k, v in ctx.headers.items()}
        verify_elevenlabs_style_signature(
            body=ctx.body,
            header_value=headers.get("elevenlabs-signature"),
            secret=_webhook_secret(secret),
            tolerance_seconds=tolerance_seconds,
        )

    def parse_webhook(self, ctx: WebhookContext) -> WebhookParseResult:
        try:
            payload = json.loads(ctx.body or b"{}")
        except (json.JSONDecodeError, ValueError) as exc:
            raise VoiceProtocolError("the ElevenLabs callback body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise VoiceProtocolError("the ElevenLabs callback body must be a JSON object")
        data = payload.get("data") or {}
        metadata = data.get("metadata") or {}
        analysis = data.get("analysis") or {}
        chars = analysis.get("transcript_summary_characters")
        return WebhookParseResult(
            provider_event_id=_opt_str(data.get("conversation_id") or payload.get("event_id")),
            session_ended=str(payload.get("type") or "") == "post_call_transcription",
            usage_characters=int(chars) if isinstance(chars, int) else None,
            duration_ms=_opt_int_ms(metadata.get("call_duration_secs")),
        )


def _api_key(secret: Any) -> str:
    if not isinstance(secret, SecretMaterial):
        raise VoiceProviderError("no ElevenLabs credential is available")
    try:
        return secret.field("api_key")
    except IntegrationCredentialUnavailableError as exc:
        raise VoiceProviderError("the ElevenLabs credential has no api_key field") from exc


def _webhook_secret(secret: Any) -> str:
    from nexus_ai.voice.errors import VoiceWebhookInvalidError

    if not isinstance(secret, SecretMaterial):
        raise VoiceWebhookInvalidError("the account has no webhook secret configured")
    try:
        return secret.field("webhook_secret")
    except IntegrationCredentialUnavailableError as exc:
        raise VoiceWebhookInvalidError("the account has no webhook secret configured") from exc


def _qp(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def _seq(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)[:200]


def _opt_int_ms(value: Any) -> int | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return int(value * 1000)
    return None
