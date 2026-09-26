"""Dedicated Sentinel platform persistence; no tenant authority or external dispatch."""

from alembic import op

revision = "d20a0b1c2d3e"
down_revision = "d19d1b1c2d3e"
branch_labels = None
depends_on = None

_TABLES = [
    "sentinel_control_state",
    "sentinel_incidents",
    "sentinel_runbooks",
    "sentinel_action_proposals",
    "sentinel_findings",
    "sentinel_signal_receipts",
    "sentinel_approvals",
    "sentinel_executions",
]

_DDL = (
    """CREATE TABLE sentinel_control_state (
	id INTEGER NOT NULL,
	revision BIGINT NOT NULL,
	mutable_actions_enabled BOOLEAN NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_sentinel_control_state PRIMARY KEY (id),
	CONSTRAINT ck_sentinel_control_state_singleton CHECK (id = 1),
	CONSTRAINT ck_sentinel_control_state_revision_positive CHECK (revision > 0)
)""",
    """CREATE TABLE sentinel_incidents (
	id UUID NOT NULL,
	correlation_key VARCHAR(64) NOT NULL,
	subject_kind VARCHAR(16) NOT NULL,
	subject_id UUID NOT NULL,
	organization_id UUID,
	state VARCHAR(24) NOT NULL,
	severity VARCHAR(8) NOT NULL,
	revision BIGINT NOT NULL,
	summary VARCHAR(256) NOT NULL,
	resolution_source VARCHAR(32),
	first_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_sentinel_incidents PRIMARY KEY (id),
	CONSTRAINT uq_sentinel_incidents_correlation_key UNIQUE (correlation_key),
	CONSTRAINT ck_sentinel_incidents_revision_positive CHECK (revision > 0),
    CONSTRAINT ck_sentinel_incidents_state_known CHECK (state IN
        ('OPEN','TRIAGED','MITIGATION_PROPOSED','MITIGATING','MONITORING','RESOLVED','CLOSED')),
    CONSTRAINT ck_sentinel_incidents_subject_known CHECK (subject_kind IN
        ('GLOBAL','SERVICE','CELL','ORGANIZATION')),
    CONSTRAINT ck_sentinel_incidents_severity_known CHECK (severity IN
        ('INFO','WARNING','ERROR','CRITICAL')),
    CONSTRAINT ck_sentinel_incidents_resolution_known CHECK (resolution_source IS NULL OR
        resolution_source IN ('TRUSTED_RECOVERY','AUTHORIZED_OPERATOR')),
    CONSTRAINT ck_sentinel_incidents_resolution_required CHECK (state NOT IN
        ('RESOLVED','CLOSED') OR resolution_source IS NOT NULL)
)""",
    """CREATE INDEX ix_sentinel_incidents_state ON sentinel_incidents (state)""",
    """CREATE TABLE sentinel_runbooks (
	id UUID NOT NULL,
	key VARCHAR(64) NOT NULL,
	revision INTEGER NOT NULL,
	handler_key VARCHAR(64) NOT NULL,
	risk VARCHAR(16) NOT NULL,
	definition JSONB NOT NULL,
	semantic_digest VARCHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_sentinel_runbooks PRIMARY KEY (id),
	CONSTRAINT uq_sentinel_runbooks_key UNIQUE (key, revision),
	CONSTRAINT uq_sentinel_runbooks_id UNIQUE (id, revision),
	CONSTRAINT ck_sentinel_runbooks_revision_positive CHECK (revision > 0),
	CONSTRAINT ck_sentinel_runbooks_key_safe CHECK (key ~ '^[a-zA-Z0-9_.:-]{1,64}$'),
	CONSTRAINT ck_sentinel_runbooks_handler_safe CHECK (handler_key ~ '^[a-zA-Z0-9_.:-]{1,64}$'),
    CONSTRAINT ck_sentinel_runbooks_risk_known CHECK (risk IN
        ('OBSERVE','DIAGNOSTIC','REVERSIBLE','HIGH_IMPACT','DESTRUCTIVE')),
    CONSTRAINT ck_sentinel_runbooks_definition_bounded CHECK (octet_length(definition::text) <=
        8192),
	CONSTRAINT ck_sentinel_runbooks_definition_object CHECK (jsonb_typeof(definition) = 'object')
)""",
    """CREATE TABLE sentinel_action_proposals (
	id UUID NOT NULL,
	incident_id UUID NOT NULL,
	incident_revision BIGINT NOT NULL,
	runbook_id UUID NOT NULL,
	runbook_revision INTEGER NOT NULL,
	policy_revision BIGINT NOT NULL,
	fingerprint VARCHAR(64) NOT NULL,
	payload JSONB NOT NULL,
	state VARCHAR(16) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_sentinel_action_proposals PRIMARY KEY (id),
	CONSTRAINT uq_sentinel_action_proposals_fingerprint UNIQUE (fingerprint),
	CONSTRAINT uq_sentinel_action_proposals_id UNIQUE (id, fingerprint, policy_revision),
    CONSTRAINT fk_sentinel_action_proposals_runbook_id_sentinel_runbooks FOREIGN KEY(runbook_id,
        runbook_revision) REFERENCES sentinel_runbooks (id, revision) ON DELETE RESTRICT,
    CONSTRAINT ck_sentinel_action_proposals_state_known CHECK (state IN
        ('PROPOSED','APPROVED','REJECTED','EXPIRED','EXECUTING','SUCCEEDED','FAILED','AMBIGUOUS','CANCELLED')),
    CONSTRAINT ck_sentinel_action_proposals_revisions_positive CHECK (incident_revision > 0 AND
        policy_revision > 0),
    CONSTRAINT ck_sentinel_action_proposals_payload_bounded CHECK (octet_length(payload::text)
        <= 8192),
    CONSTRAINT ck_sentinel_action_proposals_payload_object CHECK (jsonb_typeof(payload) =
        'object'),
	CONSTRAINT ck_sentinel_action_proposals_expiry_valid CHECK (expires_at > created_at),
    CONSTRAINT fk_sentinel_action_proposals_incident_id_sentinel_incidents FOREIGN
        KEY(incident_id) REFERENCES sentinel_incidents (id) ON DELETE RESTRICT
)""",
    """CREATE INDEX ix_sentinel_action_proposals_incident_id ON sentinel_action_proposals
        (incident_id)""",
    """CREATE TABLE sentinel_findings (
	id UUID NOT NULL,
	incident_id UUID NOT NULL,
	revision INTEGER NOT NULL,
	payload JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_sentinel_findings PRIMARY KEY (id),
	CONSTRAINT uq_sentinel_findings_incident_id UNIQUE (incident_id, revision),
	CONSTRAINT ck_sentinel_findings_revision_bounded CHECK (revision > 0 AND revision <= 100),
	CONSTRAINT ck_sentinel_findings_payload_bounded CHECK (octet_length(payload::text) <= 16384),
	CONSTRAINT ck_sentinel_findings_payload_object CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT fk_sentinel_findings_incident_id_sentinel_incidents FOREIGN KEY(incident_id)
        REFERENCES sentinel_incidents (id) ON DELETE RESTRICT
)""",
    """CREATE TABLE sentinel_signal_receipts (
	id UUID NOT NULL,
	adapter_id VARCHAR(64) NOT NULL,
	adapter_revision INTEGER NOT NULL,
	source_identity VARCHAR(64) NOT NULL,
	source_observation_id VARCHAR(64) NOT NULL,
	semantic_digest VARCHAR(64) NOT NULL,
	payload JSONB NOT NULL,
	schema_version INTEGER NOT NULL,
	status VARCHAR(16) NOT NULL,
	incident_id UUID,
	received_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_sentinel_signal_receipts PRIMARY KEY (id),
    CONSTRAINT uq_sentinel_signal_receipts_adapter_id UNIQUE (adapter_id, source_identity,
        source_observation_id),
	CONSTRAINT ck_sentinel_signal_receipts_revision_positive CHECK (adapter_revision > 0),
	CONSTRAINT ck_sentinel_signal_receipts_schema_known CHECK (schema_version = 1),
    CONSTRAINT ck_sentinel_signal_receipts_status_known CHECK (status IN
        ('RECORDED','CORRELATED')),
    CONSTRAINT ck_sentinel_signal_receipts_payload_bounded CHECK (octet_length(payload::text) <=
        32768),
    CONSTRAINT ck_sentinel_signal_receipts_payload_object CHECK (jsonb_typeof(payload) =
        'object'),
    CONSTRAINT fk_sentinel_signal_receipts_incident_id_sentinel_incidents FOREIGN
        KEY(incident_id) REFERENCES sentinel_incidents (id) ON DELETE RESTRICT
)""",
    """CREATE INDEX ix_sentinel_signal_receipts_incident_id ON sentinel_signal_receipts
        (incident_id)""",
    """CREATE TABLE sentinel_approvals (
	id UUID NOT NULL,
	proposal_id UUID NOT NULL,
	proposal_fingerprint VARCHAR(64) NOT NULL,
	approver_principal UUID NOT NULL,
	decision VARCHAR(8) NOT NULL,
	reason_code VARCHAR(64) NOT NULL,
	policy_revision BIGINT NOT NULL,
	issued_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_sentinel_approvals PRIMARY KEY (id),
	CONSTRAINT uq_sentinel_approvals_proposal_id UNIQUE (proposal_id, approver_principal),
    CONSTRAINT fk_sentinel_approvals_proposal_id_sentinel_action_proposals FOREIGN
        KEY(proposal_id, proposal_fingerprint, policy_revision) REFERENCES
        sentinel_action_proposals (id, fingerprint, policy_revision) ON DELETE RESTRICT,
	CONSTRAINT ck_sentinel_approvals_decision_known CHECK (decision IN ('APPROVED','REJECTED')),
	CONSTRAINT ck_sentinel_approvals_revision_positive CHECK (policy_revision > 0),
	CONSTRAINT ck_sentinel_approvals_expiry_valid CHECK (expires_at > issued_at)
)""",
    """CREATE INDEX ix_sentinel_approvals_proposal_id ON sentinel_approvals (proposal_id)""",
    """CREATE TABLE sentinel_executions (
	id UUID NOT NULL,
	proposal_id UUID NOT NULL,
	execution_generation BIGINT NOT NULL,
	owner_id UUID NOT NULL,
	lease_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	idempotency_key VARCHAR(128) NOT NULL,
	dispatch_state VARCHAR(16) NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	completed_at TIMESTAMP WITH TIME ZONE,
	result_classification VARCHAR(64),
	error_classification VARCHAR(64),
	external_reference UUID,
	CONSTRAINT pk_sentinel_executions PRIMARY KEY (id),
	CONSTRAINT uq_sentinel_executions_proposal_id UNIQUE (proposal_id),
	CONSTRAINT uq_sentinel_executions_idempotency_key UNIQUE (idempotency_key),
	CONSTRAINT ck_sentinel_executions_generation_positive CHECK (execution_generation > 0),
    CONSTRAINT ck_sentinel_executions_state_known CHECK (dispatch_state IN
        ('CLAIMED','DISPATCHED','COMPLETED','AMBIGUOUS')),
	CONSTRAINT ck_sentinel_executions_lease_valid CHECK (lease_expires_at > started_at),
    CONSTRAINT ck_sentinel_executions_completion_valid CHECK (completed_at IS NULL OR
        completed_at >= started_at),
    CONSTRAINT fk_sentinel_executions_proposal_id_sentinel_action_proposals FOREIGN
        KEY(proposal_id) REFERENCES sentinel_action_proposals (id) ON DELETE RESTRICT
)""",
)


def upgrade() -> None:
    for statement in _DDL:
        op.execute(statement)
    op.execute(
        "INSERT INTO sentinel_control_state (id, revision, mutable_actions_enabled) "
        "VALUES (1, 1, false)"
    )
    for table in _TABLES:
        op.execute(f'REVOKE ALL ON TABLE "{table}" FROM PUBLIC, nexus_runtime')
        op.execute(f'GRANT SELECT ON TABLE "{table}" TO nexus_sentinel')
        if table not in {"sentinel_control_state", "sentinel_executions"}:
            op.execute(f'GRANT INSERT ON TABLE "{table}" TO nexus_sentinel')
        if table in {"sentinel_control_state", "sentinel_incidents", "sentinel_signal_receipts"}:
            op.execute(f'GRANT UPDATE ON TABLE "{table}" TO nexus_sentinel')
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY')
        op.execute(
            f'CREATE POLICY sentinel_identity ON "{table}" TO nexus_sentinel '
            "USING (current_user = 'nexus_sentinel') "
            "WITH CHECK (current_user = 'nexus_sentinel')"
        )
    op.execute("""
        CREATE FUNCTION sentinel_reject_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'immutable Sentinel record' USING ERRCODE = '23514';
        END $$;
    """)
    for table in (
        "sentinel_runbooks",
        "sentinel_findings",
        "sentinel_approvals",
        "sentinel_action_proposals",
    ):
        op.execute(
            f'CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON "{table}" '
            "FOR EACH ROW EXECUTE FUNCTION sentinel_reject_mutation()"
        )
    op.execute("""
        CREATE FUNCTION sentinel_incident_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.id <> OLD.id OR NEW.correlation_key <> OLD.correlation_key
               OR NEW.subject_kind <> OLD.subject_kind OR NEW.subject_id <> OLD.subject_id
               OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
               OR NEW.first_seen_at <> OLD.first_seen_at
               OR NEW.revision <> OLD.revision + 1 THEN
                RAISE EXCEPTION 'invalid incident revision or identity' USING ERRCODE = '23514';
            END IF;
            IF NEW.state <> OLD.state AND NOT (
                (OLD.state = 'OPEN' AND NEW.state = 'TRIAGED') OR
                (OLD.state = 'TRIAGED' AND NEW.state = 'MITIGATION_PROPOSED') OR
                (OLD.state = 'MITIGATION_PROPOSED' AND NEW.state = 'MITIGATING') OR
                (OLD.state = 'MITIGATING' AND NEW.state = 'MONITORING') OR
                (OLD.state = 'MONITORING' AND NEW.state = 'RESOLVED'
                 AND NEW.resolution_source IN ('TRUSTED_RECOVERY','AUTHORIZED_OPERATOR')) OR
                (OLD.state = 'RESOLVED' AND NEW.state = 'CLOSED')
            ) THEN
                RAISE EXCEPTION 'invalid incident transition' USING ERRCODE = '23514';
            END IF;
            IF OLD.state = NEW.state AND
               NEW.resolution_source IS DISTINCT FROM OLD.resolution_source THEN
                RAISE EXCEPTION 'invalid resolution mutation' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER incident_transition BEFORE UPDATE ON sentinel_incidents
        FOR EACH ROW EXECUTE FUNCTION sentinel_incident_transition();
    """)
    op.execute("""
        CREATE FUNCTION sentinel_control_revision() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.id <> OLD.id OR NEW.revision <> OLD.revision + 1 THEN
                RAISE EXCEPTION 'invalid policy revision' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER control_revision BEFORE UPDATE ON sentinel_control_state
        FOR EACH ROW EXECUTE FUNCTION sentinel_control_revision();
    """)
    op.execute("""
        CREATE FUNCTION sentinel_receipt_transition() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF (to_jsonb(NEW) - 'status' - 'incident_id') <>
               (to_jsonb(OLD) - 'status' - 'incident_id') OR
               OLD.status <> 'RECORDED' OR NEW.status <> 'CORRELATED' OR
               OLD.incident_id IS NOT NULL OR NEW.incident_id IS NULL THEN
                RAISE EXCEPTION 'immutable signal identity' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER receipt_transition BEFORE UPDATE ON sentinel_signal_receipts
        FOR EACH ROW EXECUTE FUNCTION sentinel_receipt_transition();
    """)


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_table(table)
    for function in (
        "sentinel_receipt_transition",
        "sentinel_control_revision",
        "sentinel_incident_transition",
        "sentinel_reject_mutation",
    ):
        op.execute(f'DROP FUNCTION "{function}"()')
