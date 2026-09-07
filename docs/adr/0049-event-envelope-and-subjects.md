# ADR-0049: Canonical event envelope, subject taxonomy and versioning

Status: Accepted (NXS-P04).

Context: Without one envelope and one subject scheme, every domain invents its own shape and consumers parse untrusted blobs. Tenant identity smuggled through a payload field is a cross-tenant escalation risk.

Decision:

- **One envelope** (`nexus_ai.events.EventEnvelope`, `schema_version` `1.0`, immutable, `extra="forbid"`): `event_id` (UUIDv7, globally unique, time-ordered), `event_type` (lowercase dotted namespaced identifier), `event_version` (explicit payload version), `occurred_at` (timezone-aware, normalised to UTC), `scope` (`tenant` | `global`), `organization_id` (required for `tenant`, forbidden for `global`), `aggregate_type`/`aggregate_id`, `correlation_id`/`causation_id`/`trace_id`, `producer`, `payload`, `metadata` (bounded string map).
- **Tenant authority** is `organization_id` on the envelope, written from **trusted server-side context** by the producer (via the outbox, under RLS `WITH CHECK`). A consumer establishes its database scope from the verified envelope; if a payload also carries `organization_id` it must match, or the message is terminal. Row-Level Security remains the final boundary.
- **Subject taxonomy**: `nxs.<environment>.<scope>.<domain>.<event>`. Built and parsed only through `nexus_ai.events.subjects`; segments are validated against strict patterns; subjects never carry a tenant id, email, phone or any caller-entered value; publish subjects reject `*`/`>`; `subscribe_filter` is the only producer of a controlled trailing `>`.
- **Schema/version registry** (`nexus_ai.events.registry`): typed Pydantic payload models keyed by `(event_type, version)`. Unknown event types and unsupported versions fail closed. Payload models are `extra="forbid"`. Evolution rules: additive optional fields are compatible within a version; a required field, a removed field or a changed meaning requires a new payload version; multiple versions may be registered at once and consumers declare which they understand.
- **Provenance seam**: P04 events on the main stream are trusted internally-produced events. External webhooks (P07+) are a separate, untrusted ingestion boundary and are not treated as internal events.

Consequences: every future phase speaks the same event language; contract tests pin the envelope field set and the registered platform event types; the subject scheme is operationally filterable without leaking PII.
