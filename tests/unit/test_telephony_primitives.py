"""Telephony primitives: the call state machine, destination canonicalization, bounded
account configuration, and strict request models (NXS-P11)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from pydantic import ValidationError

from nexus_ai.core.config import TelephonySettings
from nexus_ai.telephony.account_config import validate_account_configuration
from nexus_ai.telephony.destinations import (
    DestinationKind,
    canonicalize_destination,
    canonicalize_e164,
    safe_display_name,
)
from nexus_ai.telephony.entities import (
    CallState,
    CreateCallRequest,
    RegisterPhoneNumberRequest,
    SendDtmfRequest,
    TelephonyProvider,
)
from nexus_ai.telephony.errors import (
    TelephonyConfigInvalidError,
    TelephonyInvalidDestinationError,
)
from nexus_ai.telephony.idempotency import outbound_call_fingerprint
from nexus_ai.telephony.state_machine import FoldOutcome, fold_state, is_terminal, state_rank

_SETTINGS = TelephonySettings()
_UUID = "01a08800-0000-7000-8000-000000000001"
_ACCOUNT_A = uuid.UUID("01a08800-0000-7000-8000-0000000000a1")
_ACCOUNT_B = uuid.UUID("01a08800-0000-7000-8000-0000000000b2")
_NUMBER_A = uuid.UUID("01a08800-0000-7000-8000-0000000000c3")
_NUMBER_B = uuid.UUID("01a08800-0000-7000-8000-0000000000d4")


def _fold(current: CallState, proposed: CallState, **kw: object) -> object:
    return fold_state(
        current=current,
        current_rank=state_rank(current),
        current_provider_ts=kw.get("current_ts"),  # type: ignore[arg-type]
        proposed=proposed,
        proposed_provider_ts=kw.get("proposed_ts"),  # type: ignore[arg-type]
        proposed_sequence=kw.get("proposed_seq"),  # type: ignore[arg-type]
        current_sequence=kw.get("current_seq"),  # type: ignore[arg-type]
    )


def test_forward_transitions_apply_and_infer_reordered_intermediates() -> None:
    assert _fold(CallState.CREATED, CallState.RINGING).outcome is FoldOutcome.APPLIED
    # ANSWERED arriving before a (delayed) RINGING is still a legal forward move
    assert _fold(CallState.CREATED, CallState.ANSWERED).outcome is FoldOutcome.APPLIED
    assert _fold(CallState.RINGING, CallState.BRIDGED).outcome is FoldOutcome.APPLIED
    result = _fold(CallState.ANSWERED, CallState.COMPLETED)
    assert result.outcome is FoldOutcome.APPLIED
    assert result.disposition is not None


def test_stale_and_duplicate_events_are_ignored_not_rolled_back() -> None:
    # a late RINGING after ANSWERED does not move state backward
    assert _fold(CallState.ANSWERED, CallState.RINGING).outcome is FoldOutcome.IGNORED
    # a duplicate of the current state
    assert _fold(CallState.RINGING, CallState.RINGING).outcome is FoldOutcome.IGNORED


def test_terminal_states_are_absorbing() -> None:
    for terminal in (CallState.COMPLETED, CallState.FAILED, CallState.BUSY, CallState.NO_ANSWER):
        assert is_terminal(terminal)
        # a delayed RINGING / ANSWERED after a terminal is a no-op
        assert _fold(terminal, CallState.RINGING).outcome is FoldOutcome.IGNORED
        assert _fold(terminal, CallState.ANSWERED).outcome is FoldOutcome.IGNORED
        # a second, different terminal never overwrites the first
        assert _fold(terminal, CallState.COMPLETED).state is terminal


def test_same_rank_events_use_timestamp_precedence() -> None:
    early = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    late = dt.datetime(2026, 1, 2, tzinfo=dt.UTC)
    # COMPLETED then a same-rank FAILED that is OLDER -> keep COMPLETED
    r = _fold(CallState.COMPLETED, CallState.FAILED, current_ts=late, proposed_ts=early)
    assert r.outcome is FoldOutcome.IGNORED and r.state is CallState.COMPLETED


def test_provider_sequence_precedence_is_enforced() -> None:
    # A higher-rank BRIDGED whose provider sequence is BELOW the recorded ANSWERED
    # sequence is a reordered stale callback -> ignored, state does not advance.
    stale = _fold(CallState.ANSWERED, CallState.BRIDGED, current_seq=5, proposed_seq=3)
    assert stale.outcome is FoldOutcome.IGNORED
    assert stale.state is CallState.ANSWERED
    assert stale.reason == "stale-provider-order"

    # A genuinely newer BRIDGED (higher sequence) advances the call.
    fresh = _fold(CallState.ANSWERED, CallState.BRIDGED, current_seq=5, proposed_seq=9)
    assert fresh.outcome is FoldOutcome.APPLIED
    assert fresh.state is CallState.BRIDGED

    # Absent sequences, a strictly older provider timestamp is the tie-breaker.
    early = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    late = dt.datetime(2026, 1, 2, tzinfo=dt.UTC)
    ts_stale = _fold(CallState.ANSWERED, CallState.BRIDGED, current_ts=late, proposed_ts=early)
    assert ts_stale.outcome is FoldOutcome.IGNORED and ts_stale.state is CallState.ANSWERED

    # A terminal outcome is exempt: it always wins regardless of provider order.
    terminal = _fold(CallState.ANSWERED, CallState.COMPLETED, current_seq=5, proposed_seq=1)
    assert terminal.outcome is FoldOutcome.APPLIED and terminal.state is CallState.COMPLETED


def test_outbound_call_fingerprint_binds_every_semantic_field() -> None:
    base = {
        "provider_account_id": _ACCOUNT_A,
        "from_number_id": _NUMBER_A,
        "destination": "+14155550199",
        "metadata": {"campaign": "x", "team": "y"},
    }
    fp = outbound_call_fingerprint(**base)

    # Identical request (metadata key order does not matter) -> same fingerprint.
    assert fp == outbound_call_fingerprint(
        provider_account_id=_ACCOUNT_A,
        from_number_id=_NUMBER_A,
        destination="+14155550199",
        metadata={"team": "y", "campaign": "x"},
    )

    # Any semantic change -> different fingerprint.
    assert fp != outbound_call_fingerprint(**{**base, "provider_account_id": _ACCOUNT_B})
    assert fp != outbound_call_fingerprint(**{**base, "from_number_id": _NUMBER_B})
    assert fp != outbound_call_fingerprint(**{**base, "destination": "+14155550188"})
    assert fp != outbound_call_fingerprint(**{**base, "metadata": {"campaign": "z", "team": "y"}})
    assert fp != outbound_call_fingerprint(**{**base, "metadata": {}})


@pytest.mark.parametrize(
    ("raw", "kind", "value"),
    [
        ("+14155550123", DestinationKind.PHONE, "+14155550123"),
        ("004915155550123", DestinationKind.PHONE, "+4915155550123"),
        ("4155550123", DestinationKind.PHONE, "+14155550123"),
        ("agent_desk", DestinationKind.SIP_ALIAS, "agent_desk"),
    ],
)
def test_canonicalize_destination_accepts_phone_and_alias(
    raw: str, kind: DestinationKind, value: str
) -> None:
    result = canonicalize_destination(raw, default_country="1")
    assert result.kind is kind
    assert result.value == value


@pytest.mark.parametrize(
    "raw",
    [
        "sip:evil@attacker.example",
        "+1415555\r\nRoute: <sip:evil>",
        "1234;branch=z9hG4bK",
        "<sip:x>",
        "alice@example.com",
        "+" + "9" * 40,
        "  ",
        "\x00\x01",
    ],
)
def test_canonicalize_destination_rejects_injection_and_malformed(raw: str) -> None:
    with pytest.raises(TelephonyInvalidDestinationError):
        canonicalize_destination(raw, default_country="1")


def test_canonicalize_e164_requires_a_phone_number() -> None:
    assert canonicalize_e164("+14155550123", default_country="1") == "+14155550123"
    with pytest.raises(TelephonyInvalidDestinationError):
        canonicalize_e164("agent_desk", default_country="1")


def test_safe_display_name_strips_control_and_header_chars() -> None:
    assert safe_display_name('Al"ice<x>\r\n') == "Alicex"
    assert safe_display_name(None) is None
    assert safe_display_name("   ") is None


def test_account_configuration_is_allow_listed_and_bounded() -> None:
    ok = validate_account_configuration(
        TelephonyProvider.ASTERISK,
        {"ari_base": "https://asterisk.internal:8088/ari", "stasis_app": "nexus"},
        settings=_SETTINGS,
    )
    assert ok["ari_base"] == "https://asterisk.internal:8088/ari"
    with pytest.raises(TelephonyConfigInvalidError):
        validate_account_configuration(
            TelephonyProvider.ASTERISK, {"unknown_key": "x"}, settings=_SETTINGS
        )
    with pytest.raises(TelephonyConfigInvalidError):
        validate_account_configuration(
            TelephonyProvider.ASTERISK, {"ari_base": "http://asterisk"}, settings=_SETTINGS
        )
    with pytest.raises(TelephonyConfigInvalidError):
        validate_account_configuration(
            TelephonyProvider.ASTERISK,
            {"ari_base": "https://user:pw@asterisk/ari"},
            settings=_SETTINGS,
        )


def test_create_call_request_is_strict_and_has_no_raw_from() -> None:
    ok = CreateCallRequest(
        provider_account_id=_UUID,  # type: ignore[arg-type]
        from_number_id=_UUID,  # type: ignore[arg-type]
        destination="+14155550123",
    )
    assert ok.idempotency_key is None
    assert "from" not in CreateCallRequest.model_fields
    with pytest.raises(ValidationError):
        CreateCallRequest(
            provider_account_id=_UUID,  # type: ignore[arg-type]
            from_number_id=_UUID,  # type: ignore[arg-type]
            destination="+14155550123",
            sip_headers={"From": "spoof"},  # type: ignore[call-arg]
        )


def test_dtmf_request_rejects_non_digits() -> None:
    assert SendDtmfRequest(digits="12*#A").digits == "12*#A"
    with pytest.raises(ValidationError):
        SendDtmfRequest(digits="12;3")
    with pytest.raises(ValidationError):
        SendDtmfRequest(digits="hello")


def test_register_number_request_requires_e164() -> None:
    RegisterPhoneNumberRequest(account_id=_UUID, e164="+14155550123")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        RegisterPhoneNumberRequest(account_id=_UUID, e164="4155550123")  # type: ignore[arg-type]
