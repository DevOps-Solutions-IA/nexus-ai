"""Voice concurrency guarantees, proven against real PostgreSQL (NXS-P12)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text

from nexus_ai.voice.entities import (
    CreateVoiceProfileRequest,
    StartVoiceSessionRequest,
    StopVoiceSessionRequest,
)
from nexus_ai.voice.errors import VoiceIdempotencyConflictError, VoiceMediaNotReadyError
from tests.integration.test_voice_service import ready_voice_call

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_concurrent_identical_start_is_one_logical_session(
    voice_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account, profile, call_id, media_id = await ready_voice_call(voice_stack, org.id)
    voice_stack.script["frames"] = ['{"type": "session_ended"}']

    async def _one() -> Any:
        return await voice_stack.service.start_session(
            org.id,
            StartVoiceSessionRequest(
                call_id=call_id,
                media_session_id=media_id,
                provider_account_id=account.id,
                voice_profile_id=profile.id,
                idempotency_key="voice-race-key-1",
            ),
        )

    results = await asyncio.gather(*(_one() for _ in range(8)), return_exceptions=True)
    ok = [r for r in results if not isinstance(r, Exception)]
    assert len(ok) == 8
    assert len({r.id for r in ok}) == 1
    assert not any(isinstance(r, VoiceIdempotencyConflictError) for r in results)
    await voice_stack.service.join(ok[0].id)

    async with voice_stack.database.tenant_transaction(org.id) as tenant:
        rows = (
            await tenant.session.execute(
                text("SELECT count(*) FROM voice_sessions WHERE idempotency_key = :k"),
                {"k": "voice-race-key-1"},
            )
        ).scalar_one()
    assert rows == 1


async def test_concurrent_two_starts_on_one_media_session_yield_one_live_session(
    voice_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account, profile, call_id, media_id = await ready_voice_call(voice_stack, org.id)
    voice_stack.script["frames"] = ['{"type": "agent_response", "text": "hi"}']
    voice_stack.script["timeout_after"] = 1

    async def _one(key: str) -> Any:
        return await voice_stack.service.start_session(
            org.id,
            StartVoiceSessionRequest(
                call_id=call_id,
                media_session_id=media_id,
                provider_account_id=account.id,
                voice_profile_id=profile.id,
                idempotency_key=key,
            ),
        )

    results = await asyncio.gather(
        _one("vkey-aaa1"),
        _one("vkey-bbb2"),
        _one("vkey-ccc3"),
        _one("vkey-ddd4"),
        return_exceptions=True,
    )
    ok = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, VoiceMediaNotReadyError)]
    assert len(ok) == 1 and len(conflicts) == 3, [type(r).__name__ for r in results]

    async with voice_stack.database.tenant_transaction(org.id) as tenant:
        live = (
            await tenant.session.execute(
                text(
                    "SELECT count(*) FROM voice_sessions WHERE media_session_id = :m "
                    "AND state IN ('PENDING','CONNECTING','CONNECTED','STREAMING','ENDING')"
                ),
                {"m": media_id},
            )
        ).scalar_one()
    assert live == 1
    await voice_stack.service.stop_session(org.id, ok[0].id, StopVoiceSessionRequest())


async def test_same_key_different_profile_is_a_deterministic_conflict(
    voice_stack: Any, make_organization: Any
) -> None:
    org = await make_organization()
    account, profile, call_id, media_id = await ready_voice_call(voice_stack, org.id)
    other = await voice_stack.service.create_profile(
        org.id,
        CreateVoiceProfileRequest(
            account_id=account.id,
            slug="other-profile",
            display_name="Other",
            provider_voice_ref="agent_other",
            input_format={"codec": "pcm_16000", "sample_rate": 16_000},
            output_format={"codec": "pcm_16000", "sample_rate": 16_000},
        ),
    )
    voice_stack.script["frames"] = ['{"type": "session_ended"}']
    first = await voice_stack.service.start_session(
        org.id,
        StartVoiceSessionRequest(
            call_id=call_id,
            media_session_id=media_id,
            provider_account_id=account.id,
            voice_profile_id=profile.id,
            idempotency_key="voice-key-x",
        ),
    )
    await voice_stack.service.join(first.id)
    with pytest.raises(VoiceIdempotencyConflictError):
        await voice_stack.service.start_session(
            org.id,
            StartVoiceSessionRequest(
                call_id=call_id,
                media_session_id=media_id,
                provider_account_id=account.id,
                voice_profile_id=other.id,
                idempotency_key="voice-key-x",
            ),
        )
