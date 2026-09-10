"""A deterministic fake voice provider (NXS-P12).

No socket, no credential. Exercises the SAME ``VoiceProviderAdapter`` contract as the
ElevenLabs adapter so the whole voice control + streaming surface can be tested without a
real provider. Its wire format is a trivial JSON protocol; its frames map 1:1 to the
normalized :class:`VoiceProviderEvent` kinds.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from nexus_ai.voice.entities import (
    TranscriptRole,
    VoiceProviderEvent,
    VoiceProviderEventKind,
)
from nexus_ai.voice.errors import VoiceProtocolError, VoiceWebhookInvalidError
from nexus_ai.voice.providers.base import (
    HttpError,
    ProviderSessionInit,
    VoiceHttpTransport,
    VoiceSessionSpec,
    WebhookContext,
    WebhookParseResult,
    provider_rest_error,
    verify_elevenlabs_style_signature,
)

_KIND_BY_TYPE = {
    "session_started": VoiceProviderEventKind.SESSION_STARTED,
    "session_ended": VoiceProviderEventKind.SESSION_ENDED,
    "audio": VoiceProviderEventKind.AUDIO_OUTPUT,
    "user_transcript": VoiceProviderEventKind.TRANSCRIPT,
    "agent_response": VoiceProviderEventKind.AGENT_TEXT,
    "interruption": VoiceProviderEventKind.INTERRUPTION,
    "ping": VoiceProviderEventKind.KEEPALIVE,
    "error": VoiceProviderEventKind.ERROR,
}


class FakeVoiceProvider:
    key = "fake"

    async def open_session(
        self, spec: VoiceSessionSpec, secret: Any, http: VoiceHttpTransport
    ) -> ProviderSessionInit:
        # Exercise the REST leg through the governed transport so tests cover it, but
        # tolerate a transport that has nothing programmed.
        try:
            await http.request(
                method="POST",
                url="https://fake.voice.local/v1/sessions",
                headers={"Content-Type": "application/json"},
                body=json.dumps({"voice": spec.provider_voice_ref}).encode(),
                timeout_seconds=None,
            )
        except HttpError as exc:
            if exc.timeout:
                raise provider_rest_error("the fake provider timed out", status_code=504) from exc
            raise provider_rest_error("the fake provider REST leg failed", status_code=502) from exc
        return ProviderSessionInit(
            url="wss://fake.voice.local/v1/stream",
            headers={},
            negotiated_format=spec.output_format,
            ws_host="fake.voice.local",
            ws_port=443,
            provider_session_id=f"fake-sess-{spec.external_account_id[:8]}",
        )

    def serialize_init(self, spec: VoiceSessionSpec) -> str:
        return json.dumps(
            {
                "type": "init",
                "voice": spec.provider_voice_ref,
                "input_format": spec.input_format.codec.value,
                "output_format": spec.output_format.codec.value,
            }
        )

    def serialize_audio(self, pcm: bytes) -> str:
        return json.dumps({"type": "audio_in", "chunk": base64.b64encode(pcm).decode("ascii")})

    def keepalive_reply(self, sequence: int | None) -> str:
        message: dict[str, Any] = {"type": "pong"}
        if sequence is not None:
            message["event_id"] = sequence
        return json.dumps(message)

    def parse_frame(self, raw: bytes | str) -> VoiceProviderEvent | None:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise VoiceProtocolError("the provider frame is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise VoiceProtocolError("the provider frame must be a JSON object")
        kind = _KIND_BY_TYPE.get(str(payload.get("type") or ""))
        if kind is None:
            raise VoiceProtocolError(f"unknown provider frame type {payload.get('type')!r}")

        seq = payload.get("sequence")
        common = {
            "kind": kind,
            "provider_event_id": _opt_str(payload.get("event_id")),
            "provider_sequence": int(seq) if isinstance(seq, int) else None,
        }
        if kind is VoiceProviderEventKind.AUDIO_OUTPUT:
            chunk = payload.get("chunk")
            if not isinstance(chunk, str):
                raise VoiceProtocolError("an audio frame is missing its chunk")
            try:
                audio = base64.b64decode(chunk, validate=True)
            except (ValueError, TypeError) as exc:
                raise VoiceProtocolError("an audio chunk is not valid base64") from exc
            return VoiceProviderEvent(**common, audio=audio)
        if kind in (VoiceProviderEventKind.TRANSCRIPT, VoiceProviderEventKind.AGENT_TEXT):
            text = str(payload.get("text") or "")[:8_192]
            role = (
                TranscriptRole.USER
                if kind is VoiceProviderEventKind.TRANSCRIPT
                else TranscriptRole.AGENT
            )
            return VoiceProviderEvent(
                **common, text=text, role=role, is_final=bool(payload.get("final", True))
            )
        if kind is VoiceProviderEventKind.SESSION_STARTED:
            return VoiceProviderEvent(
                **common, provider_session_id=_opt_str(payload.get("session_id"))
            )
        if kind is VoiceProviderEventKind.ERROR:
            label = str(payload.get("reason") or "error")[:200]
            return VoiceProviderEvent(**common, error_label=label)
        return VoiceProviderEvent(**common)

    def verify_webhook(self, ctx: WebhookContext, secret: Any, *, tolerance_seconds: int) -> None:
        headers = {k.lower(): v for k, v in ctx.headers.items()}
        verify_elevenlabs_style_signature(
            body=ctx.body,
            header_value=headers.get("x-voice-signature"),
            secret=_webhook_secret(secret),
            tolerance_seconds=tolerance_seconds,
        )

    def parse_webhook(self, ctx: WebhookContext) -> WebhookParseResult:
        try:
            payload = json.loads(ctx.body or b"{}")
        except (json.JSONDecodeError, ValueError) as exc:
            raise VoiceProtocolError("the callback body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise VoiceProtocolError("the callback body must be a JSON object")
        return WebhookParseResult(
            provider_event_id=_opt_str(payload.get("event_id")),
            session_ended=str(payload.get("type") or "") == "post_call",
            usage_characters=_opt_int(payload.get("characters")),
            duration_ms=_opt_int(payload.get("duration_ms")),
        )


def _webhook_secret(secret: Any) -> str:
    from nexus_ai.integrations.errors import IntegrationCredentialUnavailableError

    if secret is None:
        raise VoiceWebhookInvalidError("the account has no webhook secret configured")
    try:
        return str(secret.field("webhook_secret"))
    except IntegrationCredentialUnavailableError as exc:
        raise VoiceWebhookInvalidError("the account has no webhook secret configured") from exc


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)[:200]


def _opt_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None
