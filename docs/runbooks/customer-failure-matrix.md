# Customer / Conversation Failure Matrix (NXS-P06)

| # | Case | Expected result | Retryable | DB state | Event state | Isolation | Operator action |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A | Invalid email | 422 `NXS_CUSTOMER_IDENTITY_INVALID` | N/A | nothing | none | n/a | correct input |
| B | Invalid phone | 422 (ambiguous without country → fail closed) | N/A | nothing | none | n/a | provide E.164 or country |
| C | Unsupported identity type | 422 `NXS_CUSTOMER_IDENTITY_UNSUPPORTED` | N/A | nothing | none | n/a | use supported type |
| D | Identity already linked, same customer | idempotent success (existing identity) | N/A | unchanged | none new | own tenant | none |
| E | Identity linked to different customer | 409 `NXS_CUSTOMER_IDENTITY_CONFLICT` | terminal for that link | unchanged | none | own tenant | unlink first (future seam) |
| F | Same identity concurrent create | one Customer; others resolve to it | replay-safe | one identity row | one customers.created | own tenant | none |
| G | Same identity across two tenants | two independent Customers | N/A | two rows | two events | fully isolated | none |
| H | Customer not found | 404 `NXS_CUSTOMER_NOT_FOUND` | N/A | unchanged | none | 404 never discloses foreign tenants | none |
| I | Forged customer_id | 404 (no cross-tenant disclosure) | N/A | unchanged | none | RLS + composite FK | none |
| J | Cross-tenant identity link | fails at DB (composite FK) | N/A | unchanged | none | enforced | audit |
| K | External conversation duplicate | resolves to existing Conversation | replay-safe | one conversation | one opened event | own tenant | none |
| L | Invalid conversation transition | 409 `NXS_CONVERSATION_STATE_CONFLICT` | N/A | unchanged | none | own tenant | none |
| M | Concurrent conversation open (same thread) | exactly one Conversation | replay-safe | one row | one event | own tenant | none |
| N | Concurrent close | idempotent CLOSED | N/A | one close | one closed event | own tenant | none |
| O | DB failure mid-create | full rollback | retry same payload | nothing | none | unchanged | retry |
| P | Outbox failure | whole business transaction rolls back | retry | nothing | none | unchanged | retry |
| Q | NATS outage after commit | domain state committed; outbox PENDING | automatic | committed | published on recovery | intact | none (relay recovers) |
| R | Event replay | broker dedup + consumer idempotency | N/A | unchanged | no duplicate effect | intact | none |
| S | Malformed stored timeline data | JSONB validated on write; corrupted rows fail timeline reads closed | operator repair | row | n/a | own tenant | repair/regenerate |
| T | Stale/revoked principal | 401 before business logic (P03 live-state) | N/A | unchanged | none | intact | re-authenticate |
| U | Suspended Organization | 403 before business logic | N/A | unchanged | none | intact | reactivate org |
| V | Identity normalization collision | same normalized value resolves to the SAME Customer (deterministic) | N/A | unchanged | none | own tenant | none — by design |
