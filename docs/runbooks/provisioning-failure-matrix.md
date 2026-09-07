# Provisioning Failure Matrix (NXS-P05)

For every case: expected state, retryable/terminal, API behavior, DB state, event/
outbox state, tenant-isolation result and operator action.

| # | Case | Expected state / API | Retryable | DB state | Event state | Operator action |
| --- | --- | --- | --- | --- | --- | --- |
| A | Invalid onboarding input | 422 (field errors; extra fields forbidden incl. `organization_id`, `owner_role`) | N/A | nothing written | none | correct the input |
| B | Unauthorized creator | 403 `NXS_CORE_PERMISSION_DENIED` (permission `organization:create`) | N/A | nothing | none | grant the platform capability |
| C | Duplicate idempotency request (same key+payload) | 201 with the ORIGINAL result | N/A | one org, one COMPLETED row | none new | none |
| D | Same key / different payload | 409 `NXS_PROV_IDEMPOTENCY_CONFLICT` | terminal for that key | existing row untouched | none | new key |
| E | Duplicate slug | 409 `NXS_ORG_CONFLICT` (winner unaffected) | new key only | FAILED row recorded (post-claim path) | none | choose a new slug |
| F | Concurrent duplicate provisioning | exactly one Organization; others replay/in-progress | in-progress is retryable | one org | one outbox row | none |
| G | DB failure before commit | 503-class; whole Phase-2 rolled back | retry same key | PENDING row only | none | retry |
| H | DB commit succeeds / response lost | retry returns the original result (replay) | N/A | COMPLETED | one outbox row | none |
| I | Outbox insert failure | whole Phase-2 rolled back (audit rule) | retry same key after resume window | PENDING row only | none | retry |
| J | NATS unavailable after DB commit | provisioning unaffected; outbox PENDING | N/A | COMPLETED org + PENDING outbox | published on recovery | none (relay auto-recovers) |
| K | Provisioning retry | deterministic per C–J | per case | per case | per case | per case |
| L | Initial owner assignment conflict | structurally impossible (fresh org, unique constraints) | N/A | N/A | N/A | N/A |
| M | Forged role | 422 (unknown field) / owner role fixed to `org_owner` | N/A | none | none | none |
| N | Forged tenant | 422 (unknown field); scope always server-generated | N/A | none | none | none |
| O | RLS rejection | cross-tenant reads return zero rows; WITH CHECK inserts fail at the DB | N/A | unchanged | unchanged | none |
| P | Malformed dashboard config | 422 `NXS_DASH_SCHEMA_INVALID` / 409 unknown-widget | operator data fix | stored row invalid | none | repair or regenerate |
| Q | Unknown dashboard widget | 409 `NXS_DASH_UNKNOWN_WIDGET` (fail closed) | fix the registry/config | unchanged | none | audit |
| R | Unsupported dashboard schema version | 409 `NXS_DASH_UNSUPPORTED_VERSION` | upgrade/migrate | unchanged | none | migrate schema |
| S | Tenant grants itself capabilities | structurally impossible: registry-controlled permissions; platform capability is user-granted only | N/A | unchanged | none | none |
| T | Runtime process crash/restart | resume per ADR-0050 (nothing / finalize / stale-resume) | deterministic | per crash point | per crash point | none (automatic) |
