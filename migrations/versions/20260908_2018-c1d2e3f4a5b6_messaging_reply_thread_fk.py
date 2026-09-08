"""messaging reply-thread self-reference FK (NXS-P09 audit corrective C)

Adds a COMPOSITE TENANT-AWARE self-reference foreign key on ``messaging_messages``:
``(organization_id, reply_to_message_id) -> messaging_messages(organization_id, id)``.
The database now refuses a forged cross-tenant or dangling reply relation; a new root
message keeps ``reply_to_message_id`` NULL (PostgreSQL MATCH SIMPLE does not enforce the
FK when the referencing column is NULL). ``ON DELETE SET NULL`` keeps a future message
purge safe.

Revision ID: c1d2e3f4a5b6
Revises: b92b586b3e23
Create Date: 2026-09-08 20:18:00+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "b92b586b3e23"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FK = "fk_messaging_messages_org_reply_parent"


def upgrade() -> None:
    op.create_foreign_key(
        _FK,
        "messaging_messages",
        "messaging_messages",
        ["organization_id", "reply_to_message_id"],
        ["organization_id", "id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(_FK, "messaging_messages", type_="foreignkey")
