"""Workflow configuration cannot become an execution-policy escape hatch."""

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from nexus_ai.workflows.entities import WorkflowStepSpec


@pytest.mark.parametrize("kind", ["SHELL", "SQL", "ARBITRARY_HTTP", "CRON", "HUMAN_QUEUE"])
def test_dangerous_or_deferred_step_types_are_rejected(kind: str) -> None:
    with pytest.raises(ValidationError):
        WorkflowStepSpec.model_validate(
            {"key": "escape", "step_type": kind, "config": {"kind": kind}}
        )


def test_tool_step_cannot_carry_url_or_credentials() -> None:
    with pytest.raises(ValidationError):
        WorkflowStepSpec.model_validate(
            {
                "key": "escape",
                "step_type": "TOOL",
                "config": {
                    "kind": "TOOL",
                    "tool_key": "crm.safe",
                    "arguments": {},
                    "url": "https://attacker.invalid",
                    "authorization": "secret",
                },
            }
        )


def test_agent_step_cannot_select_provider_or_model() -> None:
    with pytest.raises(ValidationError):
        WorkflowStepSpec.model_validate(
            {
                "key": "reason",
                "step_type": "AGENT",
                "config": {
                    "kind": "AGENT",
                    "agent_id": "018fae68-bbab-7c8e-8000-000000000001",
                    "prompt": "safe input",
                    "provider": "openai",
                    "api_key": "secret",
                },
            }
        )


@pytest.mark.anyio
@pytest.mark.integration
async def test_published_version_is_database_immutable(workflow_stack, make_organization) -> None:
    from nexus_ai.workflows.entities import (
        CreateWorkflowRequest,
        NoopStepConfig,
        WorkflowStepSpec,
        WorkflowStepType,
    )

    organization = await make_organization()
    definition = await workflow_stack.service.create_definition(
        organization.id,
        CreateWorkflowRequest(
            workflow_key="immutable.version",
            name="Immutable",
            steps=(
                WorkflowStepSpec(
                    key="only",
                    step_type=WorkflowStepType.NOOP,
                    config=NoopStepConfig(output={}),
                ),
            ),
        ),
    )
    version = await workflow_stack.service.publish(organization.id, definition.id)
    with pytest.raises(DBAPIError):
        async with workflow_stack.database.tenant_transaction(organization.id) as tenant:
            await tenant.session.execute(
                text("UPDATE workflow_versions SET content_hash='mutated' WHERE id=:id"),
                {"id": version.id},
            )
