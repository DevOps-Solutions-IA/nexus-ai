# ADR-0029: Organization is the canonical tenant

Status: Accepted. Context: multi-tenant systems drift into competing identifiers (account, workspace, company, client). Decision: the customer-facing term is **Organization**, the internal technical term is **tenant**, and tenant ownership always resolves to `organization_id`. Future entities may carry their own ids, but every tenant-scoped row's ownership is an `organization_id` foreign key to `organizations.id`. Consequences: one boundary to reason about, test and secure; no P02 code, migration or policy references any other tenancy key.
