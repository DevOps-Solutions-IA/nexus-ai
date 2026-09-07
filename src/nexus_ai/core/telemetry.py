"""Vendor-neutral telemetry boundary (NXS-TELEMETRY-001).

P01 only establishes the permanent instrumentation seam: a configurable tracer provider
(``disabled`` / ``local`` / ``export``), trace-context propagation and a helper to read
the active trace id so logs and traces correlate. Dashboards and collectors belong to P24.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from nexus_ai.core.config import Settings

if TYPE_CHECKING:
    from fastapi import FastAPI

_provider: TracerProvider | None = None


def configure_telemetry(settings: Settings) -> None:
    global _provider
    if _provider is not None or settings.telemetry.mode == "disabled":
        # "disabled" leaves the OpenTelemetry no-op provider in place: zero overhead,
        # trace context still propagates, and logs simply carry no trace id.
        return
    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.namespace": settings.telemetry.service_namespace,
            "service.version": settings.build.commit,
            "deployment.environment": str(settings.environment),
        }
    )
    sampler = ParentBased(TraceIdRatioBased(settings.telemetry.sample_ratio))
    provider = TracerProvider(resource=resource, sampler=sampler)

    if settings.telemetry.mode == "local":
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    elif settings.telemetry.mode == "export":
        provider.add_span_processor(BatchSpanProcessor(_otlp_exporter(settings)))

    if trace.get_tracer_provider().__class__.__name__ in {
        "ProxyTracerProvider",
        "NoOpTracerProvider",
        "_DefaultTracerProvider",
    }:
        trace.set_tracer_provider(provider)
    _provider = provider


def _otlp_exporter(settings: Settings) -> SpanExporter:
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=settings.telemetry.otlp_endpoint)


def instrument_app(app: FastAPI) -> None:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, exclude_spans=["receive", "send"])


def shutdown_telemetry() -> None:
    global _provider
    if _provider is not None:
        _provider.shutdown()
        _provider = None


def current_trace_id() -> str | None:
    span = trace.get_current_span()
    context = span.get_span_context()
    if not context.is_valid:
        return None
    return format(context.trace_id, "032x")
