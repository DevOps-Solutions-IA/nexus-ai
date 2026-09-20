"""Minimal DID discovery projection with exact P11 source binding."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from nexus_ai.infrastructure.orm import TENANT_OWNED, TENANT_SCOPED_KEY, Base, TenantOwnedMixin


class SipDidLocatorRecord(TenantOwnedMixin, Base):
    __tablename__ = "sip_did_locators"
    __table_args__: Any = (
        UniqueConstraint("e164"),
        UniqueConstraint("phone_number_id"),
        UniqueConstraint("organization_id", "id", name="uq_sip_did_locators_org_id"),
        ForeignKeyConstraint(
            ["organization_id", "phone_number_id", "account_id", "e164"],
            [
                "telephony_phone_numbers.organization_id",
                "telephony_phone_numbers.id",
                "telephony_phone_numbers.account_id",
                "telephony_phone_numbers.e164",
            ],
            ondelete="CASCADE",
            onupdate="CASCADE",
        ),
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint(r"e164 ~ '^\+[1-9][0-9]{6,14}$'", name="e164_form"),
        {"info": {TENANT_SCOPED_KEY: TENANT_OWNED}},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    phone_number_id: Mapped[UUID] = mapped_column()
    account_id: Mapped[UUID] = mapped_column(index=True)
    e164: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(BigInteger)
    active: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CellSipTargetRecord(Base):
    __tablename__ = "cell_sip_targets"
    __table_args__ = (
        UniqueConstraint("cell_id", "target_revision", name="uq_cell_sip_target_revision"),
        UniqueConstraint("cell_id", "id", name="uq_cell_sip_target_identity"),
        CheckConstraint("target_revision > 0", name="revision_positive"),
        CheckConstraint("port BETWEEN 1024 AND 65535", name="port_bounded"),
        CheckConstraint("transport IN ('UDP','TCP','TLS')", name="transport_known"),
        CheckConstraint(
            "state IN ('REGISTERED','ACTIVE','DRAINING','RETIRED')", name="state_known"
        ),
        Index(
            "uq_cell_sip_target_active",
            "cell_id",
            unique=True,
            postgresql_where=text("state = 'ACTIVE'"),
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    cell_id: Mapped[UUID] = mapped_column(ForeignKey("cells.id", ondelete="RESTRICT"))
    target_revision: Mapped[int] = mapped_column(BigInteger)
    host: Mapped[str] = mapped_column(String(45))
    port: Mapped[int] = mapped_column()
    transport: Mapped[str] = mapped_column(String(3))
    state: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CellSipTargetHeadRecord(Base):
    __tablename__ = "cell_sip_target_heads"
    __table_args__ = (
        CheckConstraint("control_revision >= 0", name="revision_nonnegative"),
        ForeignKeyConstraint(
            ["cell_id", "active_target_id"],
            ["cell_sip_targets.cell_id", "cell_sip_targets.id"],
            ondelete="RESTRICT",
        ),
    )
    cell_id: Mapped[UUID] = mapped_column(
        ForeignKey("cells.id", ondelete="RESTRICT"), primary_key=True
    )
    control_revision: Mapped[int] = mapped_column(BigInteger)
    active_target_id: Mapped[UUID | None] = mapped_column()


class SipTargetMutationRecord(Base):
    __tablename__ = "sip_target_mutations"
    __table_args__ = (
        UniqueConstraint("operation", "key_hash"),
        CheckConstraint(
            "operation IN ('REGISTER','ACTIVE','DRAINING','RETIRED')", name="operation_known"
        ),
        CheckConstraint("result_revision > expected_revision", name="revision_advanced"),
        ForeignKeyConstraint(
            ["cell_id", "target_id"],
            ["cell_sip_targets.cell_id", "cell_sip_targets.id"],
            ondelete="RESTRICT",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    cell_id: Mapped[UUID] = mapped_column()
    target_id: Mapped[UUID] = mapped_column()
    operation: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    expected_revision: Mapped[int] = mapped_column(BigInteger)
    result_revision: Mapped[int] = mapped_column(BigInteger)
    result_state: Mapped[str] = mapped_column(String(16))
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    reason_code: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[UUID] = mapped_column()
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SipEdgeAuthHeadRecord(Base):
    __tablename__ = "sip_edge_auth_heads"
    edge_id: Mapped[UUID] = mapped_column(primary_key=True)


class SipEdgeReplayRecord(Base):
    __tablename__ = "sip_edge_replays"
    __table_args__ = (
        CheckConstraint("nonce_digest ~ '^[a-f0-9]{64}$'", name="nonce_digest_valid"),
        CheckConstraint("request_digest ~ '^[a-f0-9]{64}$'", name="request_digest_valid"),
        Index("ix_sip_edge_replays_expiry", "edge_id", "expires_at"),
    )
    edge_id: Mapped[UUID] = mapped_column(
        ForeignKey("sip_edge_auth_heads.edge_id", ondelete="RESTRICT"), primary_key=True
    )
    nonce_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    boot_id: Mapped[UUID] = mapped_column()
    request_digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SipRouteAuthorizationRecord(TenantOwnedMixin, Base):
    __tablename__ = "sip_route_authorizations"
    __table_args__: Any = (
        UniqueConstraint("organization_id", "id", name="uq_sip_routes_org_id"),
        UniqueConstraint("peer_id", "transaction_digest", name="uq_sip_routes_transaction"),
        ForeignKeyConstraint(
            ["organization_id", "phone_number_id", "account_id", "e164"],
            [
                "telephony_phone_numbers.organization_id",
                "telephony_phone_numbers.id",
                "telephony_phone_numbers.account_id",
                "telephony_phone_numbers.e164",
            ],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "placement_id"],
            ["organization_placements.organization_id", "organization_placements.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["cell_id", "target_id"],
            ["cell_sip_targets.cell_id", "cell_sip_targets.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("placement_generation > 0 AND target_revision > 0", name="positive_fences"),
        CheckConstraint(
            "state IN ('AUTHORIZED','ISSUED','ESTABLISHED','ENDED','FAILED','AMBIGUOUS','EXPIRED')",
            name="state_known",
        ),
        CheckConstraint(
            "issue_deadline > authorized_at "
            "AND issue_deadline <= authorized_at + interval '5 seconds'",
            name="issue_window",
        ),
        CheckConstraint(
            "transaction_digest ~ '^[a-f0-9]{64}$' AND semantic_digest ~ '^[a-f0-9]{64}$'",
            name="digests_valid",
        ),
        {"info": {TENANT_SCOPED_KEY: TENANT_OWNED}},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    peer_id: Mapped[UUID] = mapped_column()
    phone_number_id: Mapped[UUID] = mapped_column()
    account_id: Mapped[UUID] = mapped_column()
    e164: Mapped[str] = mapped_column(String(16))
    placement_id: Mapped[UUID] = mapped_column()
    cell_id: Mapped[UUID] = mapped_column()
    placement_generation: Mapped[int] = mapped_column(BigInteger)
    target_id: Mapped[UUID] = mapped_column()
    target_revision: Mapped[int] = mapped_column(BigInteger)
    transaction_digest: Mapped[str] = mapped_column(String(64))
    semantic_digest: Mapped[str] = mapped_column(String(64))
    edge_id: Mapped[UUID] = mapped_column()
    boot_id: Mapped[UUID] = mapped_column()
    state: Mapped[str] = mapped_column(String(16))
    authorized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    issue_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SipTargetRouteReferenceRecord(Base):
    __tablename__ = "sip_target_route_references"
    route_id: Mapped[UUID] = mapped_column(
        ForeignKey("sip_route_authorizations.id", ondelete="RESTRICT"), primary_key=True
    )
    target_id: Mapped[UUID] = mapped_column(
        ForeignKey("cell_sip_targets.id", ondelete="RESTRICT"), index=True
    )


class SipRouteHistoryRecord(TenantOwnedMixin, Base):
    __tablename__ = "sip_route_history"
    __table_args__: Any = (
        ForeignKeyConstraint(
            ["organization_id", "route_id"],
            ["sip_route_authorizations.organization_id", "sip_route_authorizations.id"],
            ondelete="RESTRICT",
        ),
        {"info": {TENANT_SCOPED_KEY: TENANT_OWNED}},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    route_id: Mapped[UUID] = mapped_column(index=True)
    previous_state: Mapped[str | None] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16))
    edge_id: Mapped[UUID] = mapped_column()
    boot_id: Mapped[UUID] = mapped_column()
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SipPeerProfileRecord(Base):
    __tablename__ = "sip_peer_profiles"
    __table_args__ = (
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("octet_length(profile::text) <= 16384", name="profile_bounded"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger)
    profile: Mapped[dict[str, Any]] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column()


class SipPeerHistoryRecord(Base):
    __tablename__ = "sip_peer_history"
    peer_id: Mapped[UUID] = mapped_column(
        ForeignKey("sip_peer_profiles.id", ondelete="RESTRICT"), primary_key=True
    )
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    profile: Mapped[dict[str, Any]] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column()
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SipUpstreamRecord(Base):
    __tablename__ = "sip_upstreams"
    __table_args__ = (
        CheckConstraint("revision > 0", name="revision_positive"),
        CheckConstraint("port BETWEEN 1024 AND 65535", name="port_bounded"),
        CheckConstraint("transport IN ('UDP','TCP','TLS')", name="transport_known"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    host: Mapped[str] = mapped_column(String(45))
    port: Mapped[int] = mapped_column()
    transport: Mapped[str] = mapped_column(String(3))
    cell_id: Mapped[UUID] = mapped_column(ForeignKey("cells.id", ondelete="RESTRICT"))
    asterisk_peer_id: Mapped[UUID] = mapped_column()


class SipAccountUpstreamRecord(TenantOwnedMixin, Base):
    __tablename__ = "sip_account_upstreams"
    __table_args__: Any = (
        ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["telephony_accounts.organization_id", "telephony_accounts.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["upstream_id", "upstream_revision"],
            ["sip_upstreams.id", "sip_upstreams.revision"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("revision > 0", name="revision_positive"),
        {"info": {TENANT_SCOPED_KEY: TENANT_OWNED}},
    )
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    upstream_id: Mapped[UUID] = mapped_column()
    upstream_revision: Mapped[int] = mapped_column(BigInteger)
    revision: Mapped[int] = mapped_column(BigInteger)


class SipEgressPermitRecord(TenantOwnedMixin, Base):
    __tablename__ = "sip_egress_permits"
    __table_args__: Any = (
        UniqueConstraint("organization_id", "id", name="uq_sip_egress_org_id"),
        UniqueConstraint("organization_id", "call_id", name="uq_sip_egress_call"),
        UniqueConstraint("token_digest"),
        ForeignKeyConstraint(
            ["organization_id", "call_id"],
            ["telephony_calls.organization_id", "telephony_calls.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["telephony_accounts.organization_id", "telephony_accounts.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["upstream_id", "upstream_revision"],
            ["sip_upstreams.id", "sip_upstreams.revision"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "placement_id"],
            ["organization_placements.organization_id", "organization_placements.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "state IN ('AUTHORIZED','CONSUMED','ENDED','AMBIGUOUS','EXPIRED','REVOKED')",
            name="state_known",
        ),
        CheckConstraint("placement_generation > 0", name="generation_positive"),
        CheckConstraint("expires_at = issued_at + interval '30 seconds'", name="ttl_fixed"),
        CheckConstraint(
            "token_digest ~ '^[a-f0-9]{64}$' AND destination_digest ~ '^[a-f0-9]{64}$' "
            "AND semantic_digest ~ '^[a-f0-9]{64}$'",
            name="digests_valid",
        ),
        {"info": {TENANT_SCOPED_KEY: TENANT_OWNED}},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    call_id: Mapped[UUID] = mapped_column()
    account_id: Mapped[UUID] = mapped_column()
    placement_id: Mapped[UUID] = mapped_column()
    cell_id: Mapped[UUID] = mapped_column(ForeignKey("cells.id", ondelete="RESTRICT"))
    placement_generation: Mapped[int] = mapped_column(BigInteger)
    upstream_id: Mapped[UUID] = mapped_column()
    upstream_revision: Mapped[int] = mapped_column(BigInteger)
    asterisk_peer_id: Mapped[UUID] = mapped_column()
    destination_digest: Mapped[str] = mapped_column(String(64))
    token_digest: Mapped[str] = mapped_column(String(64))
    semantic_digest: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    edge_id: Mapped[UUID | None] = mapped_column()
    boot_id: Mapped[UUID | None] = mapped_column()
    transaction_digest: Mapped[str | None] = mapped_column(String(64))


class SipEgressRouteRecord(TenantOwnedMixin, Base):
    __tablename__ = "sip_egress_routes"
    __table_args__: Any = (
        ForeignKeyConstraint(
            ["organization_id", "permit_id"],
            ["sip_egress_permits.organization_id", "sip_egress_permits.id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("peer_id", "transaction_digest", name="uq_sip_egress_route_transaction"),
        {"info": {TENANT_SCOPED_KEY: TENANT_OWNED}},
    )
    permit_id: Mapped[UUID] = mapped_column(primary_key=True)
    peer_id: Mapped[UUID] = mapped_column()
    edge_id: Mapped[UUID] = mapped_column()
    boot_id: Mapped[UUID] = mapped_column()
    transaction_digest: Mapped[str] = mapped_column(String(64))
    authorized_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SipDialogBindingRecord(TenantOwnedMixin, Base):
    __tablename__ = "sip_dialog_bindings"
    __table_args__: Any = (
        ForeignKeyConstraint(
            ["organization_id", "route_id"],
            ["sip_route_authorizations.organization_id", "sip_route_authorizations.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("dialog_digest ~ '^[a-f0-9]{64}$'", name="digest_valid"),
        {"info": {TENANT_SCOPED_KEY: TENANT_OWNED}},
    )
    route_id: Mapped[UUID] = mapped_column(primary_key=True)
    dialog_digest: Mapped[str] = mapped_column(String(64))
    established_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SipEgressDialogBindingRecord(TenantOwnedMixin, Base):
    __tablename__ = "sip_egress_dialog_bindings"
    __table_args__: Any = (
        ForeignKeyConstraint(
            ["organization_id", "permit_id"],
            ["sip_egress_permits.organization_id", "sip_egress_permits.id"],
            ondelete="RESTRICT",
        ),
        CheckConstraint("dialog_digest ~ '^[a-f0-9]{64}$'", name="digest_valid"),
        {"info": {TENANT_SCOPED_KEY: TENANT_OWNED}},
    )
    permit_id: Mapped[UUID] = mapped_column(primary_key=True)
    dialog_digest: Mapped[str] = mapped_column(String(64))
    established_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SipAccountUpstreamHistoryRecord(TenantOwnedMixin, Base):
    __tablename__ = "sip_account_upstream_history"
    __table_args__: Any = (
        ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["telephony_accounts.organization_id", "telephony_accounts.id"],
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["upstream_id", "upstream_revision"],
            ["sip_upstreams.id", "sip_upstreams.revision"],
            ondelete="RESTRICT",
        ),
        {"info": {TENANT_SCOPED_KEY: TENANT_OWNED}},
    )
    account_id: Mapped[UUID] = mapped_column(primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    upstream_id: Mapped[UUID] = mapped_column()
    upstream_revision: Mapped[int] = mapped_column(BigInteger)
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SipEgressHistoryRecord(TenantOwnedMixin, Base):
    __tablename__ = "sip_egress_history"
    __table_args__: Any = (
        ForeignKeyConstraint(
            ["organization_id", "permit_id"],
            ["sip_egress_permits.organization_id", "sip_egress_permits.id"],
            ondelete="RESTRICT",
        ),
        {"info": {TENANT_SCOPED_KEY: TENANT_OWNED}},
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    permit_id: Mapped[UUID] = mapped_column(index=True)
    previous_state: Mapped[str | None] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
