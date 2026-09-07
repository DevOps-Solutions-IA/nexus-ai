"""Telemetry boundary configuration (NXS-TELEMETRY-001, section 40)."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from opentelemetry import trace

from nexus_ai.core.config import Settings
from nexus_ai.core.telemetry import (
    configure_telemetry,
    current_trace_id,
    instrument_app,
    shutdown_telemetry,
)

Build = Callable[..., Settings]


@pytest.fixture(autouse=True)
def _reset_provider():
    yield
    shutdown_telemetry()


def test_disabled_mode_is_noop(build_settings: Build) -> None:
    configure_telemetry(build_settings(NXS_TELEMETRY__MODE="disabled"))
    assert current_trace_id() is None


def test_local_mode_sets_a_real_provider_and_trace_id(build_settings: Build) -> None:
    configure_telemetry(build_settings(NXS_TELEMETRY__MODE="local"))
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("unit"):
        trace_id = current_trace_id()
        assert trace_id is not None
        assert len(trace_id) == 32
    shutdown_telemetry()
    assert current_trace_id() is None


def test_configure_is_idempotent(build_settings: Build) -> None:
    settings = build_settings(NXS_TELEMETRY__MODE="local")
    configure_telemetry(settings)
    configure_telemetry(settings)  # must not raise


def test_instrument_app_attaches_middleware(build_settings: Build) -> None:
    from nexus_ai.application import create_app

    configure_telemetry(build_settings(NXS_TELEMETRY__MODE="local"))
    app = create_app(build_settings(NXS_TELEMETRY__MODE="local"))
    instrument_app(app)  # idempotent-safe, must not raise
