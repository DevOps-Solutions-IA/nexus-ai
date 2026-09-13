"""Stable API and P04 event contracts for NXS-P14."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from nexus_ai.application import create_app
from nexus_ai.events.envelope import EventEnvelope
from nexus_ai.events.errors import EventContractError
from nexus_ai.events.registry import EVENT_REGISTRY
from nexus_ai.workflows import events as workflow_events  # noqa: F401
from nexus_ai.workflows.entities import CreateWorkflowRequest


def test_openapi_exposes_only_governed_workflow_routes() -> None:
    paths = create_app().openapi()["paths"]
    expected = {
        "/api/v1/workflows",
        "/api/v1/workflows/{definition_id}",
        "/api/v1/workflows/{definition_id}/publish",
        "/api/v1/workflows/{definition_id}/versions",
        "/api/v1/workflows/{definition_id}/versions/{version_id}",
        "/api/v1/workflow-runs",
        "/api/v1/workflow-runs/{run_id}",
        "/api/v1/workflow-runs/{run_id}/pause",
        "/api/v1/workflow-runs/{run_id}/resume",
        "/api/v1/workflow-runs/{run_id}/cancel",
        "/api/v1/workflow-runs/{run_id}/steps",
        "/api/v1/workflow-runs/{run_id}/transitions",
    }
    assert expected <= set(paths)
    assert not any(forbidden in path for path in paths for forbidden in ("/cron", "/shell", "/sql"))


def test_request_contract_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CreateWorkflowRequest.model_validate(
            {
                "workflow_key": "safe.flow",
                "name": "Safe",
                "steps": [],
                "arbitrary_http_url": "https://attacker.invalid",
            }
        )


@pytest.mark.parametrize(
    "event_type",
    [
        "workflow.definition.created",
        "workflow.version.published",
        "workflow.run.started",
        "workflow.run.paused",
        "workflow.run.resumed",
        "workflow.run.completed",
        "workflow.run.failed",
        "workflow.run.cancelled",
        "workflow.step.ready",
        "workflow.step.started",
        "workflow.step.completed",
        "workflow.step.failed",
        "workflow.step.skipped",
    ],
)
def test_workflow_event_types_are_registered(event_type: str) -> None:
    assert EVENT_REGISTRY.supported_versions(event_type) == (1,)


def test_event_payload_rejects_secret_material() -> None:
    envelope = EventEnvelope.create(
        event_type="workflow.run.started",
        event_version=1,
        aggregate_type="workflow_run",
        aggregate_id=str(uuid4()),
        organization_id=uuid4(),
        producer="nexus-ai",
        payload={
            "run_id": str(uuid4()),
            "definition_id": str(uuid4()),
            "version_id": str(uuid4()),
            "state": "RUNNING",
            "correlation_id": None,
            "api_key": "forbidden",
        },
    )
    with pytest.raises(EventContractError):
        EVENT_REGISTRY.decode(envelope)
