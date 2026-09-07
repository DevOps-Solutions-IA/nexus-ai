# ADR-0052: Customer identity model, normalization and resolution

Status: Accepted. Context: NXS-CUSTOMER-001 requires omnichannel customer identity with deterministic normalization, duplicate-safe creation and tenant-safe resolution. Decision:

**Customer aggregate** — minimal PII by design (`display_name`, validated `preferred_locale`, ACTIVE/SUSPENDED, version). No CRM constructs (deals/pipelines/tasks/notes/billing/external CRM sync).

**Identity plane** — `customer_identities` is modeled independently of the Customer core (1:N). Each identity carries `identity_type` (EMAIL/PHONE/EXTERNAL_ID), the canonical `normalized_value`, a verification state (UNVERIFIED/VERIFIED/REVOKED — P10 owns actual OTP verification; P06 provides the state model and the controlled mutation seam), an ACTIVE/REVOKED lifecycle, and a bounded provenance `source`. No provider secrets.

**Normalization** — deterministic, documented: EMAIL uses the platform's single email vocabulary (trim, lowercase, NFC, conservative ASCII — same rules as user email); PHONE canonicalizes to E.164 with an explicit `default_country` required when the number lacks a leading `+` (ambiguity fails closed — never guessed silently); EXTERNAL_ID is trim/NFC/bounded/control-char-free and NEVER globally trusted. Normalized values — and only normalized values — are what uniqueness and resolution operate on.

**Uniqueness invariant** — `UNIQUE(organization_id, identity_type, normalized_value)`: within one Organization the same ACTIVE canonical identity cannot belong to two Customers; across Organizations the same email/phone is fully independent. The unique index IS the resolution index.

**Resolution** — `CustomerIdentityResolver` (via `CustomerService.resolve_or_create`): zero matches → create Customer + identity + timeline + `customers.created` outbox event in ONE tenant transaction; one match → return the existing Customer; a race on the unique constraint re-resolves to the winner (DB constraints are the final authority — no Python locks). Ambiguity is structurally impossible (unique constraint) and would fail closed.

**Duplicate policy** — Nexus P06 prevents deterministic duplicates by EXACT canonical identity. It does NOT promise semantic duplicate detection: `john@example.com` and `john.smith@example.com` are not automatically the same person. No fuzzy/AI/LLM identity merging.

**Linking** — `link_identity` is idempotent for the same (customer, identity) and deterministically conflicts when the identity belongs to another Customer; no silent identity stealing. Cross-tenant attachment is refused at TWO levels: forced RLS (runtime) and composite tenant-aware foreign keys `(organization_id, <parent_id>) → (organization_id, id)` (schema).

**Privacy** — identity data is sensitive: minimal PII, no raw identities in logs (hashed where diagnostics need them), no bearer/session data, bounded metadata, event payloads carry IDs only (never raw email/phone), and the model keeps a clear future deletion/anonymization seam (P21 owns the lifecycle).

Consequences: later channel phases (P09+) plug providers into this model; resolution ALWAYS includes organization scope — a phone in Organization A never resolves a Customer in Organization B.
