# Customer Identity and Conversations (NXS-P06)

The omnichannel customer identity and conversation foundation. P06 builds the model;
later phases plug actual channels (WhatsApp/email/SMS in P09, telephony in P11+) into it.

## Canonical tenant rule

Every Customer and Conversation belongs to exactly one Organization —
`organization_id` remains the only tenant identity. A phone/email/external identity in
Organization A never resolves a Customer in Organization B: resolution ALWAYS includes
organization scope.

## Domain model

```
Organization
   └── Customer (display identity, locale, ACTIVE/SUSPENDED, version)
         ├── CustomerIdentity × N   (EMAIL | PHONE | EXTERNAL_ID)
         │      UNIQUE(org, type, normalized_value)  ← the identity invariant
         └── Conversation × N (channel-neutral, PENDING/OPEN/CLOSED)
                ├── ConversationParticipant × N (typed refs)
                └── ConversationActivity × N (unified timeline, UUIDv7-ordered)
```

- Identity normalization: `docs/adr/0052-customer-identity-model.md` — deterministic,
  documented, ambiguity fails closed (no silent country guessing).
- Conversation lifecycle: PENDING→OPEN/CLOSED, OPEN→CLOSED, CLOSED→OPEN (explicit
  reopen), idempotent close — `docs/adr/0053-conversation-model-and-timeline.md`.
- Duplicate policy: exact canonical identity only; no semantic/fuzzy/LLM merging.

## Services and APIs

| Endpoint | Permission | Notes |
| --- | --- | --- |
| `POST /api/v1/customers` | customer:create | resolve-or-create; 201 created / 200 resolved |
| `GET /api/v1/customers?identity_type=&identity=` | customer:read | tenant-scoped canonical lookup |
| `GET /api/v1/customers/{id}` | customer:read | 404 outside the tenant |
| `POST /api/v1/customers/{id}/identities` | customer:identity:link | idempotent; 409 on conflict |
| `GET /api/v1/customers/{id}/identities` | customer:read | |
| `GET /api/v1/customers/{id}/conversations` | conversation:read | cursor-paginated, newest first |
| `GET /api/v1/customers/{id}/timeline` | customer:timeline:read | (occurred_at, id) cursor |
| `POST /api/v1/conversations` | conversation:create | external thread key deduplicates |
| `GET /api/v1/conversations/{id}` | conversation:read | |
| `POST /api/v1/conversations/{id}/close` | conversation:close | idempotent |

No generic resolve/execute/raw-query endpoints. No message-sending endpoints (P09+).

## RBAC

P06 delta (seeded by migration `6e63fd35017a`): owner/admin hold all eight P06
permissions; org_member holds all except `customer:update`. Verification-state
mutations use the controlled internal seam only (P10 owns OTP).

## Events (P04 outbox, same transaction as the mutation)

`customers.created`, `customers.identity.linked`, `customers.identity.status_changed`,
`conversations.opened`, `conversations.closed` — payloads carry IDs only, never raw
PII.

## Isolation and failure behavior

- Forced RLS on all five P06 tables + composite tenant-aware FKs
  (`(organization_id, parent_id) → (organization_id, id)`) — cross-tenant attachment
  fails at the database.
- Failure matrix: `docs/runbooks/customer-failure-matrix.md`.
- Timeline ≠ audit log (P22); timeline ≠ outbox (P04).

## Non-scope

No channel providers, no OTP, no CRM constructs, no agent records, no fuzzy merging,
no compliance lifecycle, no full-text search.
