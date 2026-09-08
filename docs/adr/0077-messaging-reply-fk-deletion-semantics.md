# ADR-0077: Messaging reply self-reference FK — deletion semantics

Status: Accepted. Corrects a defect in migration `c1d2e3f4a5b6` (added by ADR-0076 corrective C). An independent audit found that a *plain* `ON DELETE SET NULL` on the composite tenant-aware self-reference FK `(organization_id, reply_to_message_id) → messaging_messages(organization_id, id)` would attempt to NULL **both** referencing columns when a parent message is deleted — and `messaging_messages.organization_id` is `NOT NULL`, so the delete would fail with a NOT NULL violation. The migration comment claimed parent deletion was safe with no regression proving it.

Decision:

**Column-specific `ON DELETE SET NULL (reply_to_message_id)`.** PostgreSQL 15+ supports naming exactly which referencing columns a `SET NULL` action touches. The project's PostgreSQL baseline is **17.6** (`compose.yaml`, pinned by index digest), so the form is available. SQLAlchemy passes the `ondelete` string through verbatim, so `ForeignKeyConstraint(..., ondelete="SET NULL (reply_to_message_id)")` in `messaging_messages` and the same string in migration `c1d2e3f4a5b6` emit:

```
FOREIGN KEY (organization_id, reply_to_message_id)
  REFERENCES messaging_messages(organization_id, id)
  ON DELETE SET NULL (reply_to_message_id)
```

verified against the live database (`pg_get_constraintdef`).

**Semantics.** Deleting a parent message NULLs **only** `reply_to_message_id` on its children; `organization_id` is left unchanged, so the row stays valid and tenant isolation is preserved. A new root message keeps `reply_to_message_id` NULL and — because PostgreSQL MATCH SIMPLE does not enforce a composite FK when any referencing column is NULL — the FK is inert for it. A forged cross-tenant reply relation is still refused by the FK (the corrective's `test_database_fk_prevents_a_forged_cross_tenant_reply_relation`).

**Why not `ON DELETE RESTRICT`.** RESTRICT would forbid deleting any parent with replies. The messaging subsystem has no message-deletion path (messages are append-only), so RESTRICT would only matter for a future purge / retention job — and there, silently blocking a purge because an old reply still points at a row is a worse operational outcome than cleanly nulling the pointer. Column-specific SET NULL keeps a future purge safe without weakening isolation.

**Regression** (`tests/integration/test_messaging_reply_threading.py::test_parent_deletion_nulls_only_reply_to_and_keeps_organization`): create parent + child in one Organization, `child.reply_to_message_id = parent.id`, `DELETE` the parent through a real PostgreSQL transaction (owner role, tenant GUC set) → the delete succeeds (`DELETE 1`), `child.reply_to_message_id IS NULL`, `child.organization_id` unchanged, every other child field valid, and the child is still readable through the service. Migration `c1d2e3f4a5b6` upgrade / downgrade / re-upgrade / `alembic check` all pass; tenant schema guard 0 violations.

Consequences: the reply relation is a real, database-enforced, tenant-safe foreign key whose deletion behaviour is proven and depends on the pinned PG 17 baseline; a downgrade to a pre-15 PostgreSQL would need the FK re-expressed (documented here).
