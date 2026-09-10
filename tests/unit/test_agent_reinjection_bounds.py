"""NXS-P13 audit corrective #1 — byte-exact Unicode tool-result bound (blocker 3).

``bound_reinjection`` must guarantee the FINAL re-injected object serialises within
``max_tool_result_bytes`` UTF-8 bytes for ASCII and arbitrary Unicode, wrapper overhead
included, never splitting a code point. The pre-corrective ``_clip_json`` measured UTF-8
bytes but sliced by Python characters (``encoded[: limit - 32]``) — an emoji / CJK /
combining-character payload blew straight past the limit.
"""

from __future__ import annotations

import json

import pytest

from nexus_ai.agents.toolbridge import bound_reinjection, reinjection_json

_ASCII = "A" * 60_000
_EMOJI = "\U0001f600" * 15_000  # U+1F600, 4 UTF-8 bytes; 12 bytes when \u-escaped
_CJK = "字" * 18_000  # U+5B57, 3 UTF-8 bytes
_COMBINING = "é" * 15_000  # base + combining acute, decomposed
_MIXED = "\xdcn\xefc\xf6d\xe9 — 世界 — \U0001f30d " * 4_000

_PAYLOADS = {
    "ascii": _ASCII,
    "emoji": _EMOJI,
    "cjk": _CJK,
    "combining": _COMBINING,
    "mixed": _MIXED,
}


def _serialisations(obj: object) -> list[int]:
    """Every way the object could be serialised to JSON on the way to the model."""
    return [
        len(reinjection_json(obj).encode("utf-8")),  # exactly what the runtime writes
        len(json.dumps(obj).encode("utf-8")),  # a plain json.dumps (ensure_ascii=True, spaces)
        len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")),
    ]


@pytest.mark.parametrize("name", list(_PAYLOADS))
@pytest.mark.parametrize("limit", [256, 1024, 4096, 32_768])
def test_oversized_output_is_bounded_for_every_unicode_shape(name: str, limit: int) -> None:
    source = _PAYLOADS[name]
    payload = {"ok": True, "status_code": 200, "result_class": "SUCCESS", "output": source}
    bounded = bound_reinjection(payload, limit)
    for size in _serialisations(bounded):
        assert size <= limit, (name, limit, size)
    # every source here is far over the largest tested limit, so it must be truncated
    assert isinstance(bounded["output"], dict)
    assert bounded["output"]["truncated"] is True
    assert source.startswith(bounded["output"].get("preview", ""))  # a real code-point prefix


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_bound_holds_around_the_exact_limit(delta: int) -> None:
    limit = 2_048
    base = {"ok": True, "status_code": 200, "result_class": "SUCCESS", "output": ""}
    overhead = len(reinjection_json(base).encode("utf-8")) - len('""')
    body = "世" * ((limit - overhead + delta) // len("世".encode()))
    payload = {**base, "output": body}
    bounded = bound_reinjection(payload, limit)
    for size in _serialisations(bounded):
        assert size <= limit, (delta, size)


def test_small_payloads_pass_through_untouched() -> None:
    payload = {"ok": True, "status_code": 200, "result_class": "SUCCESS", "output": {"id": "c1"}}
    assert bound_reinjection(payload, 32_768) is payload


def test_a_giant_non_string_output_is_still_bounded() -> None:
    payload = {
        "ok": True,
        "status_code": 200,
        "result_class": "SUCCESS",
        "output": [{"name": "名前", "note": "\U0001f5d2️" * 50} for _ in range(2_000)],
    }
    bounded = bound_reinjection(payload, 1_024)
    for size in _serialisations(bounded):
        assert size <= 1_024


def test_error_envelopes_always_fit_the_floor() -> None:
    for envelope in (
        {"error": "tool_not_available", "tool": "x" * 64},
        {"error": "tool_denied", "reason": "NXS_TOOL_PERMISSION_DENIED"},
        {"error": "tool_failed", "code": "NXS_INT_DESTINATION_BLOCKED"},
    ):
        bounded = bound_reinjection(envelope, 256)
        assert len(json.dumps(bounded).encode("utf-8")) <= 256
