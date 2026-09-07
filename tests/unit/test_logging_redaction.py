"""Structured logging redaction and bounds (NXS-LOG-001, MASTER PROMPT 002 section 31)."""

from __future__ import annotations

import json

import pytest

from nexus_ai.core.config import Settings
from nexus_ai.core.context import request_context
from nexus_ai.core.logging import (
    bound_fields_processor,
    configure_logging,
    context_processor,
    get_logger,
    redact_processor,
)


def test_redact_processor_masks_sensitive_keys() -> None:
    event = redact_processor(
        None,
        "info",
        {
            "event": "login",
            "password": "hunter2",
            "authorization": "Bearer abc.def",
            "cookie": "session=xyz",
            "api_key": "sk-live-1",
            "user": "alice",
        },
    )
    assert event["password"] == "«redacted»"
    assert event["authorization"] == "«redacted»"
    assert event["cookie"] == "«redacted»"
    assert event["api_key"] == "«redacted»"
    assert event["user"] == "alice"


def test_redact_processor_masks_url_credentials() -> None:
    event = redact_processor(
        None, "info", {"event": "connect", "target": "postgresql://u:s3cret@db:5432/n"}
    )
    assert "s3cret" not in event["target"]
    assert "«redacted»" in event["target"]


def test_bound_fields_processor_truncates() -> None:
    processor = bound_fields_processor(10)
    event = processor(None, "info", {"event": "x", "blob": "y" * 50})
    assert len(event["blob"]) == 10
    assert event["blob"].endswith("…")


def test_context_processor_injects_request_fields() -> None:
    with request_context(request_id="req-000000-0001", correlation_id="cor-000000-0001"):
        event = context_processor(None, "info", {"event": "x"})
    assert event["request_id"] == "req-000000-0001"
    assert event["correlation_id"] == "cor-000000-0001"


def test_json_logging_pipeline_end_to_end(
    capsys: pytest.CaptureFixture[str], build_settings
) -> None:
    settings: Settings = build_settings(NXS_LOGGING__FORMAT="json")
    configure_logging(settings)
    logger = get_logger("nexus_ai.test")
    with request_context(request_id="req-aaaaaa-0001", correlation_id="cor-aaaaaa-0001"):
        logger.info("thing_happened", password="leaked", note="ok")
    line = capsys.readouterr().out.strip().splitlines()[-1]
    record = json.loads(line)
    assert record["event"] == "thing_happened"
    assert record["severity"] == "INFO"
    assert record["service"] == settings.service_name
    assert record["request_id"] == "req-aaaaaa-0001"
    assert record["password"] == "«redacted»"
    assert "timestamp" in record
