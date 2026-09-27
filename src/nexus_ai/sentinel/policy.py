"""Advisory eligibility checks, not an execution grant or external dispatcher."""

from nexus_ai.sentinel.contracts import Risk, Runbook


def risk_allowed(
    runbook: Runbook, *, durable_approval: bool, mutable_actions_enabled: bool
) -> bool:
    if not runbook.enabled or runbook.risk in {Risk.HIGH_IMPACT, Risk.DESTRUCTIVE}:
        return False
    if runbook.risk in {Risk.OBSERVE, Risk.DIAGNOSTIC}:
        return runbook.non_mutating and runbook.policy_requirement == "READ_ONLY"
    return (
        not runbook.non_mutating
        and runbook.policy_requirement == "HUMAN_APPROVAL"
        and durable_approval
        and mutable_actions_enabled
    )
