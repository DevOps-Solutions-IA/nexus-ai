"""messaging reply-thread self-reference FK (NXS-P09 audit correctives C + FK)

Adds a COMPOSITE TENANT-AWARE self-reference foreign key on ``messaging_messages``:
``(organization_id, reply_to_message_id) -> messaging_messages(organization_id, id)``.
The database now refuses a forged cross-tenant or dangling reply relation; a new root
message keeps ``reply_to_message_id`` NULL (PostgreSQL MATCH SIMPLE does not enforce the
FK when the referencing column is NULL).

Deletion semantics: ``ON DELETE SET NULL (reply_to_message_id)`` — the PostgreSQL 15+
column-specific form. Deleting a parent message NULLs only ``reply_to_message_id`` on its
children; ``organization_id`` (NOT NULL) is left unchanged and tenant isolation is
preserved. The project's PostgreSQL baseline is 17.6 (compose.yaml), so the
column-specific list is available. A plain ``ON DELETE SET NULL`` would try to NULL every
referencing column, including the NOT NULL ``organization_id``, and would fail the delete.

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
        ondelete="SET NULL (reply_to_message_id)",
    )


def downgrade() -> None:
    op.drop_constraint(_FK, "messaging_messages", type_="foreignkey")
