"""Sentinel recurring incidents, operator grants and fenced execution writes."""

from alembic import op

revision = "d20b0b1c2d3e"
down_revision = "d20a0b1c2d3e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE sentinel_incidents DROP CONSTRAINT uq_sentinel_incidents_correlation_key"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_sentinel_incidents_active_correlation "
        "ON sentinel_incidents (correlation_key) WHERE state NOT IN ('RESOLVED','CLOSED')"
    )
    op.execute("GRANT INSERT, UPDATE ON sentinel_executions TO nexus_sentinel")
    op.execute("GRANT UPDATE (state) ON sentinel_action_proposals TO nexus_sentinel")
    op.execute("DROP TRIGGER immutable_record ON sentinel_action_proposals")
    op.execute("""
        CREATE FUNCTION sentinel_proposal_transition() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF to_jsonb(NEW) - 'state' <> to_jsonb(OLD) - 'state'
               OR NOT ((OLD.state = 'PROPOSED' AND NEW.state IN ('EXECUTING','EXPIRED','CANCELLED'))
                    OR (OLD.state = 'EXECUTING'
                        AND NEW.state IN ('SUCCEEDED','FAILED','AMBIGUOUS'))) THEN
                RAISE EXCEPTION 'invalid proposal transition' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER proposal_transition BEFORE UPDATE ON sentinel_action_proposals
        FOR EACH ROW EXECUTE FUNCTION sentinel_proposal_transition();
    """)
    op.execute("ALTER TABLE platform_grants DROP CONSTRAINT ck_platform_grants_capability_known")
    op.execute(
        "ALTER TABLE platform_grants ADD CONSTRAINT ck_platform_grants_capability_known "
        "CHECK (capability IN ('organization:create','cell:control','sip_edge:control',"
        "'sentinel:read','sentinel:triage','sentinel:approve','sentinel:control'))"
    )
    op.execute("""
        CREATE FUNCTION sentinel_execution_fence() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'durable execution slot' USING ERRCODE = '23514';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.execution_generation <> 1 OR NEW.dispatch_state <> 'CLAIMED'
                   OR NEW.completed_at IS NOT NULL THEN
                    RAISE EXCEPTION 'invalid initial execution' USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.id <> OLD.id OR NEW.proposal_id <> OLD.proposal_id
               OR NEW.idempotency_key <> OLD.idempotency_key
               OR NEW.started_at <> OLD.started_at
               OR OLD.dispatch_state IN ('COMPLETED','AMBIGUOUS') THEN
                RAISE EXCEPTION 'immutable execution identity or result' USING ERRCODE = '23514';
            END IF;
            IF NEW.execution_generation <> OLD.execution_generation THEN
                IF NEW.execution_generation <> OLD.execution_generation + 1
                   OR OLD.dispatch_state <> 'CLAIMED' OR NEW.dispatch_state <> 'CLAIMED'
                   OR OLD.lease_expires_at > clock_timestamp()
                   OR NEW.lease_expires_at <= clock_timestamp()
                   OR NEW.owner_id = OLD.owner_id THEN
                    RAISE EXCEPTION 'invalid lease takeover' USING ERRCODE = '23514';
                END IF;
            ELSIF NEW.owner_id <> OLD.owner_id
               OR NEW.lease_expires_at <> OLD.lease_expires_at
               OR NOT ((OLD.dispatch_state = 'CLAIMED'
                        AND NEW.dispatch_state IN ('DISPATCHED','COMPLETED'))
                    OR (OLD.dispatch_state = 'DISPATCHED'
                        AND NEW.dispatch_state IN ('COMPLETED','AMBIGUOUS'))) THEN
                RAISE EXCEPTION 'invalid execution transition' USING ERRCODE = '23514';
            END IF;
            IF NEW.dispatch_state = 'DISPATCHED' AND NEW.lease_expires_at <= clock_timestamp() THEN
                RAISE EXCEPTION 'expired dispatch' USING ERRCODE = '23514';
            END IF;
            IF (NEW.dispatch_state IN ('COMPLETED','AMBIGUOUS'))
               <> (NEW.completed_at IS NOT NULL) THEN
                RAISE EXCEPTION 'invalid completion' USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER execution_fence BEFORE INSERT OR UPDATE OR DELETE ON sentinel_executions
        FOR EACH ROW EXECUTE FUNCTION sentinel_execution_fence();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER proposal_transition ON sentinel_action_proposals")
    op.execute("DROP FUNCTION sentinel_proposal_transition()")
    op.execute("REVOKE UPDATE (state) ON sentinel_action_proposals FROM nexus_sentinel")
    op.execute("""
        CREATE TRIGGER immutable_record BEFORE UPDATE OR DELETE ON sentinel_action_proposals
        FOR EACH ROW EXECUTE FUNCTION sentinel_reject_mutation();
    """)
    op.execute("DROP TRIGGER execution_fence ON sentinel_executions")
    op.execute("DROP FUNCTION sentinel_execution_fence()")
    op.execute("REVOKE INSERT, UPDATE ON sentinel_executions FROM nexus_sentinel")
    op.execute("ALTER TABLE platform_grants DROP CONSTRAINT ck_platform_grants_capability_known")
    op.execute(
        "ALTER TABLE platform_grants ADD CONSTRAINT ck_platform_grants_capability_known "
        "CHECK (capability IN ('organization:create','cell:control','sip_edge:control'))"
    )
    op.execute("DROP INDEX uq_sentinel_incidents_active_correlation")
    op.execute(
        "ALTER TABLE sentinel_incidents ADD CONSTRAINT "
        "uq_sentinel_incidents_correlation_key UNIQUE (correlation_key)"
    )
