"""Voice failure-mode resilience (NXS-P12): provider unavailable, disconnect after
connect, provider timeout, malformed frame, backpressure, and deterministic cleanup."""

from __future__ import annotations

import json
from typing import Any

import pytest

from nexus_ai.voice.entities import StartVoiceSessionRequest
from nexus_ai.voice.providers.base import HttpError
from nexus_ai.voice.state_machine import VoiceSessionState
from tests.integration.test_voice_service import ready_voice_call

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def _start(voice_stack: Any, org_id: Any) -> Any:
    account, profile, call_id, media_id = await ready_voice_call(voice_stack, org_id)
    return await voice_stack.service.start_session(
        org_id,
        StartVoiceSessionRequest(
            call_id=call_id,
            media_session_id=media_id,
            provider_account_id=account.id,
            voice_profile_id=profile.id,
        ),
    )


async def test_provider_rest_leg_unavailable_fails_the_session(
    voice_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    voice_stack.http.set_handler(lambda _e: HttpError("connection refused", connect=True))
    session = await _start(voice_stack, org.id)
    await voice_stack.service.join(session.id)
    final = await voice_stack.service.get_session(org.id, session.id)
    assert final.state is VoiceSessionState.FAILED
    assert final.error_code is not None and final.error_code.startswith("NXS_VOICE_")


async def test_provider_disconnect_after_connect_completes_or_fails_cleanly(
    voice_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    voice_stack.script["frames"] = ['{"type": "session_started", "session_id": "s"}']
    voice_stack.script["close_after"] = 1  # peer closes after one frame
    session = await _start(voice_stack, org.id)
    await voice_stack.service.join(session.id)
    final = await voice_stack.service.get_session(org.id, session.id)
    assert final.state.value in ("COMPLETED", "FAILED")
    assert final.usage is not None and final.latency is not None


async def test_malformed_provider_frame_ends_the_session_as_protocol_error(
    voice_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    voice_stack.script["frames"] = ["this is not json at all"]
    voice_stack.script["timeout_after"] = 1
    session = await _start(voice_stack, org.id)
    await voice_stack.service.join(session.id)
    final = await voice_stack.service.get_session(org.id, session.id)
    assert final.state is VoiceSessionState.FAILED
    assert final.error_code == "NXS_VOICE_PROTOCOL_ERROR"


async def test_provider_idle_timeout_ends_the_session(
    voice_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    voice_stack.script["frames"] = []
    voice_stack.script["timeout_after"] = 0
    session = await _start(voice_stack, org.id)
    await voice_stack.service.join(session.id)
    final = await voice_stack.service.get_session(org.id, session.id)
    assert final.error_code == "NXS_VOICE_PROVIDER_TIMEOUT"


async def test_repeated_start_stop_cycles_do_not_leak(
    voice_stack: Any, make_organization: Any
) -> None:
    import asyncio

    org = await make_organization()
    voice_stack.script["frames"] = ['{"type": "session_ended"}']
    baseline = len(asyncio.all_tasks())
    for _ in range(6):
        account, profile, call_id, media_id = await ready_voice_call(voice_stack, org.id)
        session = await voice_stack.service.start_session(
            org.id,
            StartVoiceSessionRequest(
                call_id=call_id,
                media_session_id=media_id,
                provider_account_id=account.id,
                voice_profile_id=profile.id,
            ),
        )
        await voice_stack.service.join(session.id)
    await asyncio.sleep(0.05)
    assert len(asyncio.all_tasks()) <= baseline + 2
    assert not voice_stack.service._tasks  # no dangling runtime tasks


async def test_signed_post_call_callback_reconciles_usage_once(
    voice_stack: Any, make_organization: Any
) -> None:
    import hashlib
    import hmac
    import time

    from nexus_ai.voice.providers.base import WebhookContext

    org = await make_organization()
    account, *_ = await ready_voice_call(voice_stack, org.id)
    body = json.dumps({"type": "post_call", "event_id": "evt-1", "characters": 42}).encode()
    ts = str(int(time.time()))
    digest = hmac.new(b"voice-hook-secret", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    ctx = WebhookContext("POST", {"X-Voice-Signature": f"t={ts},v0={digest}"}, {}, body)
    first = await voice_stack.inbound.receive("fake", account.webhook_token, ctx)
    second = await voice_stack.inbound.receive("fake", account.webhook_token, ctx)
    assert first.processed == "reconciled"
    assert second.processed == "replayed"
