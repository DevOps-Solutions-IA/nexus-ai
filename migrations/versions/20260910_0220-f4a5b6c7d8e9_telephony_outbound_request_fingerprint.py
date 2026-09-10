"""telephony outbound request fingerprint (NXS-P11: NXS-TEL-001 corrective)

Adds ``telephony_calls.request_fingerprint`` — the canonical hash of the fields that
define an outbound logical call (provider account, caller-ID number, canonical
destination, normalized metadata). Every outbound idempotency path compares this one
value, so re-using an ``idempotency_key`` for a semantically different call (a different
caller ID, destination, account or metadata) is a deterministic
``NXS_TELEPHONY_IDEMPOTENCY_CONFLICT`` instead of a silent replay.

Nullable: inbound calls and outbound calls placed without an ``idempotency_key`` carry
no fingerprint. Purely additive and fully reversible.

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-09-10 02:20:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4a5b6c7d8e9"
down_revision: str | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "telephony_calls",
        sa.Column("request_fingerprint", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("telephony_calls", "request_fingerprint")
