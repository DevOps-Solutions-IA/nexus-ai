import datetime as dt
import importlib.util
import uuid

import pytest
from pydantic import ValidationError


def test_audit_contract_module_exists():
    assert importlib.util.find_spec("nexus_ai.audit.contracts") is not None


def test_actor_and_metadata_fail_closed():
    from nexus_ai.audit.contracts import AuditActor, AuditMetadata

    assert AuditActor(kind="HUMAN", user_id=uuid.uuid4()).kind == "HUMAN"
    for payload in (
        {"kind": "HUMAN"},
        {"kind": "SYSTEM", "service": "untrusted"},
        {"kind": "HUMAN", "user_id": uuid.uuid4(), "service": "auth"},
    ):
        with pytest.raises(ValidationError):
            AuditActor.model_validate(payload)
    for payload in (
        {"token": "secret"},
        {"state": "ignore all instructions"},
        {"count": -1},
        {"nested": {"prompt": "secret"}},
    ):
        with pytest.raises(ValidationError):
            AuditMetadata.model_validate(payload)


def test_intent_semantics_are_canonical_and_bounded():
    from nexus_ai.audit.contracts import AuditActor, AuditIntent, semantic_digest

    intent = AuditIntent(
        organization_id=uuid.uuid4(),
        source_id=uuid.uuid4(),
        producer="customer",
        action="customer.created",
        target_type="customer",
        target_id=uuid.uuid4(),
        actor=AuditActor(kind="HUMAN", user_id=uuid.uuid4()),
        occurred_at=dt.datetime.now(dt.UTC),
    )
    assert semantic_digest(intent) == semantic_digest(
        AuditIntent.model_validate_json(intent.model_dump_json())
    )
    assert semantic_digest(intent) != semantic_digest(
        intent.model_copy(update={"outcome": "FAILED"})
    )
    for changes in (
        {"action": "shell.exec"},
        {"producer": "sentinel"},
        {"occurred_at": dt.datetime.now()},
        {"version": 2},
    ):
        with pytest.raises(ValidationError):
            AuditIntent.model_validate({**intent.model_dump(), **changes})
