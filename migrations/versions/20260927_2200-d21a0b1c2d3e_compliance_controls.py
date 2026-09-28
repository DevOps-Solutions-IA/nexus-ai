"""Tenant compliance controls with forced RLS and immutable authorization records."""

import sqlalchemy as sa
from alembic import op

from nexus_ai.infrastructure.rls import apply_tenant_rls

revision = "d21a0b1c2d3e"
down_revision = "d20c0b1c2d3e"
branch_labels = None
depends_on = None

_TABLES = [
    "compliance_controls",
    "compliance_policies",
    "compliance_holds",
    "compliance_requests",
    "compliance_plans",
    "compliance_approvals",
    "compliance_executions",
]
_DDL = (
    """
        CREATE TABLE compliance_controls (
        id UUID NOT NULL,
        policy_epoch INTEGER NOT NULL,
        hold_epoch INTEGER NOT NULL,
        organization_id UUID NOT NULL,
        CONSTRAINT pk_compliance_controls PRIMARY KEY (id),
        CONSTRAINT uq_compliance_controls_organization_id UNIQUE (organization_id),
        CONSTRAINT ck_compliance_controls_epochs_valid CHECK (policy_epoch >= 0 AND hold_epoch
        >= 0),
        CONSTRAINT fk_compliance_controls_organization_id_organizations FOREIGN
        KEY(organization_id) REFERENCES organizations (id) ON DELETE RESTRICT
        )
    """,
    """
        CREATE TABLE compliance_policies (
        id UUID NOT NULL,
        revision INTEGER NOT NULL,
        state VARCHAR(16) NOT NULL,
        rules JSONB NOT NULL,
        activated_by UUID,
        activated_at TIMESTAMP WITH TIME ZONE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        organization_id UUID NOT NULL,
        CONSTRAINT pk_compliance_policies PRIMARY KEY (id),
        CONSTRAINT uq_compliance_policies_organization_id UNIQUE (organization_id,  id),
        CONSTRAINT uq_compliance_policy_revision UNIQUE (organization_id,  revision),
        CONSTRAINT ck_compliance_policies_revision_positive CHECK (revision > 0),
        CONSTRAINT ck_compliance_policies_state_known CHECK (state IN ('DRAFT', 'ACTIVE',
        'RETIRED')),
        CONSTRAINT ck_compliance_policies_rules_bounded CHECK (octet_length(rules::text) <=
        8192),
        CONSTRAINT fk_compliance_policies_organization_id_organizations FOREIGN
        KEY(organization_id) REFERENCES organizations (id) ON DELETE RESTRICT
        )
    """,
    """
        CREATE TABLE compliance_holds (
        id UUID NOT NULL,
        subject_id UUID NOT NULL,
        resource_class VARCHAR(32),
        state VARCHAR(16) NOT NULL,
        reason_code VARCHAR(96) NOT NULL,
        created_by UUID NOT NULL,
        released_by UUID,
        released_at TIMESTAMP WITH TIME ZONE,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        organization_id UUID NOT NULL,
        CONSTRAINT pk_compliance_holds PRIMARY KEY (id),
        CONSTRAINT uq_compliance_holds_organization_id UNIQUE (organization_id,  id),
        CONSTRAINT fk_compliance_holds_organization_id_customers FOREIGN KEY(organization_id,
        subject_id) REFERENCES customers (organization_id,  id),
        CONSTRAINT ck_compliance_holds_state_known CHECK (state IN ('ACTIVE', 'RELEASED')),
        CONSTRAINT fk_compliance_holds_organization_id_organizations FOREIGN
        KEY(organization_id) REFERENCES organizations (id) ON DELETE RESTRICT
        )
    """,
    """
        CREATE TABLE compliance_requests (
        id UUID NOT NULL,
        subject_id UUID NOT NULL,
        kind VARCHAR(16) NOT NULL,
        state VARCHAR(16) NOT NULL,
        idempotency_key VARCHAR(96) NOT NULL,
        semantic_digest VARCHAR(64) NOT NULL,
        verified_by UUID,
        verified_at TIMESTAMP WITH TIME ZONE,
        verification JSONB NOT NULL,
        incomplete_resources JSONB NOT NULL,
        result JSONB NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        organization_id UUID NOT NULL,
        CONSTRAINT pk_compliance_requests PRIMARY KEY (id),
        CONSTRAINT uq_compliance_requests_organization_id UNIQUE (organization_id,  id),
        CONSTRAINT uq_compliance_request_key UNIQUE (organization_id,  idempotency_key),
        CONSTRAINT fk_compliance_requests_organization_id_customers FOREIGN KEY(organization_id,
         subject_id) REFERENCES customers (organization_id,  id),
        CONSTRAINT ck_compliance_requests_kind_known CHECK (kind IN ('ACCESS', 'ERASURE',
        'RESTRICTION')),
        CONSTRAINT ck_compliance_requests_state_known CHECK (state IN ('RECEIVED', 'VERIFIED',
        'PLANNED', 'APPROVED', 'EXECUTING', 'COMPLETED', 'PARTIAL', 'DENIED', 'CANCELLED',
        'EXPIRED')),
        CONSTRAINT ck_compliance_requests_completion_truthful CHECK (state <> 'COMPLETED' OR
        jsonb_array_length(incomplete_resources) = 0),
        CONSTRAINT ck_compliance_requests_result_bounded CHECK (octet_length(result::text) <=
        16384),
        CONSTRAINT fk_compliance_requests_organization_id_organizations FOREIGN
        KEY(organization_id) REFERENCES organizations (id) ON DELETE RESTRICT
        )
    """,
    """
        CREATE TABLE compliance_plans (
        id UUID NOT NULL,
        request_id UUID,
        policy_id UUID NOT NULL,
        policy_revision INTEGER NOT NULL,
        policy_epoch INTEGER NOT NULL,
        hold_epoch INTEGER NOT NULL,
        subject_id UUID NOT NULL,
        target_version INTEGER NOT NULL,
        resource_class VARCHAR(32) NOT NULL,
        action VARCHAR(16) NOT NULL,
        operation_identity VARCHAR(128) NOT NULL,
        fingerprint VARCHAR(64) NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        organization_id UUID NOT NULL,
        CONSTRAINT pk_compliance_plans PRIMARY KEY (id),
        CONSTRAINT uq_compliance_plans_organization_id UNIQUE (organization_id,  id),
        CONSTRAINT uq_compliance_plan_operation UNIQUE (organization_id,  operation_identity),
        CONSTRAINT fk_compliance_plans_organization_id_compliance_requests FOREIGN
        KEY(organization_id,  request_id) REFERENCES compliance_requests (organization_id,  id),

        CONSTRAINT fk_compliance_plans_organization_id_compliance_policies FOREIGN
        KEY(organization_id,  policy_id) REFERENCES compliance_policies (organization_id,  id),
        CONSTRAINT fk_compliance_plans_organization_id_customers FOREIGN KEY(organization_id,
        subject_id) REFERENCES customers (organization_id,  id),
        CONSTRAINT ck_compliance_plans_versions_valid CHECK (target_version > 0 AND policy_epoch
        > 0 AND hold_epoch >= 0),
        CONSTRAINT ck_compliance_plans_action_known CHECK (action IN ('ACCESS', 'ANONYMIZE',
        'RESTRICT')),
        CONSTRAINT ck_compliance_plans_resource_supported CHECK (resource_class =
        'CUSTOMER_PROFILE'),
        CONSTRAINT fk_compliance_plans_organization_id_organizations FOREIGN
        KEY(organization_id) REFERENCES organizations (id) ON DELETE RESTRICT
        )
    """,
    """
        CREATE TABLE compliance_approvals (
        id UUID NOT NULL,
        plan_id UUID NOT NULL,
        fingerprint VARCHAR(64) NOT NULL,
        approved_by UUID NOT NULL,
        reason_code VARCHAR(96) NOT NULL,
        issued_at TIMESTAMP WITH TIME ZONE NOT NULL,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        organization_id UUID NOT NULL,
        CONSTRAINT pk_compliance_approvals PRIMARY KEY (id),
        CONSTRAINT uq_compliance_approvals_organization_id UNIQUE (organization_id,  plan_id),
        CONSTRAINT fk_compliance_approvals_organization_id_compliance_plans FOREIGN
        KEY(organization_id,  plan_id) REFERENCES compliance_plans (organization_id,  id),
        CONSTRAINT ck_compliance_approvals_expiry_valid CHECK (expires_at > issued_at),
        CONSTRAINT fk_compliance_approvals_organization_id_organizations FOREIGN
        KEY(organization_id) REFERENCES organizations (id) ON DELETE RESTRICT
        )
    """,
    """
        CREATE TABLE compliance_executions (
        id UUID NOT NULL,
        plan_id UUID NOT NULL,
        owner_id UUID NOT NULL,
        generation INTEGER NOT NULL,
        state VARCHAR(16) NOT NULL,
        lease_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        result JSONB NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        completed_at TIMESTAMP WITH TIME ZONE,
        organization_id UUID NOT NULL,
        CONSTRAINT pk_compliance_executions PRIMARY KEY (id),
        CONSTRAINT uq_compliance_executions_organization_id UNIQUE (organization_id,  plan_id),
        CONSTRAINT fk_compliance_executions_organization_id_compliance_plans FOREIGN
        KEY(organization_id,  plan_id) REFERENCES compliance_plans (organization_id,  id),
        CONSTRAINT ck_compliance_executions_generation_positive CHECK (generation > 0),
        CONSTRAINT ck_compliance_executions_state_known CHECK (state IN ('CLAIMED', 'COMPLETED',
        'FAILED', 'AMBIGUOUS')),
        CONSTRAINT ck_compliance_executions_result_bounded CHECK (octet_length(result::text) <=
        16384),
        CONSTRAINT fk_compliance_executions_organization_id_organizations FOREIGN
        KEY(organization_id) REFERENCES organizations (id) ON DELETE RESTRICT
        )
    """,
    """
        CREATE INDEX ix_compliance_controls_organization_id ON compliance_controls
        (organization_id)
    """,
    """
        CREATE INDEX ix_compliance_policies_organization_id ON compliance_policies
        (organization_id)
    """,
    """
        CREATE UNIQUE INDEX uq_compliance_policy_active ON compliance_policies (organization_id)
        WHERE state = 'ACTIVE'
    """,
    """
        CREATE INDEX ix_compliance_holds_organization_id ON compliance_holds (organization_id)
    """,
    """
        CREATE INDEX ix_compliance_holds_subject ON compliance_holds (organization_id,
        subject_id,  state)
    """,
    """
        CREATE INDEX ix_compliance_requests_organization_id ON compliance_requests
        (organization_id)
    """,
    """
        CREATE INDEX ix_compliance_plans_organization_id ON compliance_plans (organization_id)
    """,
    """
        CREATE INDEX ix_compliance_approvals_organization_id ON compliance_approvals
        (organization_id)
    """,
    """
        CREATE INDEX ix_compliance_executions_organization_id ON compliance_executions
        (organization_id)
    """,
)


def upgrade() -> None:
    for statement in _DDL:
        op.execute(statement)
    for table in _TABLES:
        apply_tenant_rls(op, table)
    op.execute("""
        CREATE FUNCTION compliance_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'immutable compliance decision' USING ERRCODE = '23514';
        END $$;
    """)
    for table in ("compliance_plans", "compliance_approvals"):
        op.execute(
            f"CREATE TRIGGER immutable_decision BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION compliance_immutable()"
        )
        op.execute(f"REVOKE UPDATE ON {table} FROM nexus_runtime")
    op.execute("""
        CREATE FUNCTION compliance_policy_transition() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' OR
               (to_jsonb(NEW) - ARRAY['state','activated_by','activated_at']) <>
               (to_jsonb(OLD) - ARRAY['state','activated_by','activated_at']) OR
               NOT ((OLD.state = 'DRAFT' AND NEW.state = 'ACTIVE'
                     AND NEW.activated_by IS NOT NULL AND NEW.activated_at IS NOT NULL)
                 OR (OLD.state = 'ACTIVE' AND NEW.state = 'RETIRED'
                     AND NEW.activated_by = OLD.activated_by
                     AND NEW.activated_at = OLD.activated_at)) THEN
                RAISE EXCEPTION 'invalid compliance policy transition' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute(
        "CREATE TRIGGER policy_transition BEFORE UPDATE OR DELETE ON compliance_policies "
        "FOR EACH ROW EXECUTE FUNCTION compliance_policy_transition()"
    )
    op.execute("""
        CREATE FUNCTION compliance_request_transition() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.organization_id <> OLD.organization_id OR NEW.subject_id <> OLD.subject_id
               OR NEW.kind <> OLD.kind OR NEW.idempotency_key <> OLD.idempotency_key
               OR NEW.semantic_digest <> OLD.semantic_digest
               OR NOT NEW.incomplete_resources @> OLD.incomplete_resources THEN
                RAISE EXCEPTION 'immutable request identity or coverage' USING ERRCODE = '23514';
            END IF;
            IF NEW.state <> OLD.state AND NOT (
                (OLD.state = 'RECEIVED' AND NEW.state IN
                    ('VERIFIED','DENIED','CANCELLED','EXPIRED'))
                OR (OLD.state = 'VERIFIED' AND NEW.state IN
                    ('PLANNED','DENIED','CANCELLED','EXPIRED'))
                OR (OLD.state = 'PLANNED' AND NEW.state IN
                    ('APPROVED','DENIED','CANCELLED','EXPIRED'))
                OR (OLD.state = 'APPROVED' AND NEW.state IN
                    ('EXECUTING','DENIED','CANCELLED','EXPIRED'))
                OR (OLD.state = 'EXECUTING' AND NEW.state IN ('COMPLETED','PARTIAL'))
            ) THEN
                RAISE EXCEPTION 'invalid compliance request transition' USING ERRCODE = '23514';
            END IF;
            IF NEW.state IN ('VERIFIED','PLANNED','APPROVED','EXECUTING','COMPLETED','PARTIAL')
               AND (NEW.verified_by IS NULL OR NEW.verified_at IS NULL) THEN
                RAISE EXCEPTION 'unverified request' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute(
        "CREATE TRIGGER request_transition BEFORE UPDATE ON compliance_requests "
        "FOR EACH ROW EXECUTE FUNCTION compliance_request_transition()"
    )
    op.execute("""
        CREATE FUNCTION compliance_execution_fence() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.generation <> 1 OR NEW.state <> 'CLAIMED' THEN
                    RAISE EXCEPTION 'invalid initial execution' USING ERRCODE = '23514';
                END IF;
            ELSE
                IF NEW.organization_id <> OLD.organization_id OR NEW.plan_id <> OLD.plan_id
                   OR OLD.state <> 'CLAIMED' THEN
                    RAISE EXCEPTION 'immutable execution identity/result' USING ERRCODE = '23514';
                END IF;
                IF NEW.generation <> OLD.generation THEN
                    IF NEW.generation <> OLD.generation + 1 OR NEW.state <> 'CLAIMED'
                       OR OLD.lease_expires_at > clock_timestamp()
                       OR NEW.owner_id = OLD.owner_id
                       OR NEW.lease_expires_at <= clock_timestamp() THEN
                        RAISE EXCEPTION 'invalid execution takeover' USING ERRCODE = '23514';
                    END IF;
                ELSIF NEW.owner_id <> OLD.owner_id OR NEW.lease_expires_at <> OLD.lease_expires_at
                   OR NEW.state NOT IN ('COMPLETED','FAILED','AMBIGUOUS')
                   OR OLD.lease_expires_at <= clock_timestamp() THEN
                    RAISE EXCEPTION 'stale execution owner' USING ERRCODE = '23514';
                END IF;
            END IF;
            IF (NEW.state <> 'CLAIMED') <> (NEW.completed_at IS NOT NULL) THEN
                RAISE EXCEPTION 'invalid execution completion' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute(
        "CREATE TRIGGER execution_fence BEFORE INSERT OR UPDATE ON compliance_executions "
        "FOR EACH ROW EXECUTE FUNCTION compliance_execution_fence()"
    )
    for suffix, capability in enumerate(
        ("read", "manage_policy", "manage_hold", "manage_request", "approve", "execute"),
        start=61,
    ):
        permission_id = f"b2000000-0000-7000-8000-{suffix:012x}"
        op.get_bind().execute(
            sa.text(
                "INSERT INTO permissions (id, permission_key, description, created_at) "
                "VALUES (:id, :key, 'Tenant compliance capability', now())"
            ),
            {"id": permission_id, "key": f"compliance:{capability}"},
        )
        roles = ["a1000000-0000-7000-8000-000000000001"]
        if capability in {"read", "manage_request"}:
            roles.append("a1000000-0000-7000-8000-000000000002")
        for role in roles:
            op.get_bind().execute(
                sa.text(
                    "INSERT INTO role_permissions (role_id, permission_id, created_at) "
                    "VALUES (:role, :permission, now())"
                ),
                {"role": role, "permission": permission_id},
            )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_id IN "
        "(SELECT id FROM permissions WHERE permission_key LIKE 'compliance:%')"
    )
    op.execute("DELETE FROM permissions WHERE permission_key LIKE 'compliance:%'")
    for table in reversed(_TABLES):
        op.drop_table(table)
    for function in (
        "compliance_execution_fence",
        "compliance_request_transition",
        "compliance_policy_transition",
        "compliance_immutable",
    ):
        op.execute(f"DROP FUNCTION {function}()")
