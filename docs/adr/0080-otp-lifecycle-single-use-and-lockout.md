# ADR-0080: OTP challenge lifecycle — single-use, expiry and attempt lockout

Status: Accepted. Part of NXS-P10 (`NXS-OTP-001`).

**Statuses.** `ACTIVE → {VERIFIED | EXPIRED | REVOKED | LOCKED}`. All four non-active
states are terminal; a `CHECK` constraint pins the enum and `attempts <= max_attempts`.

**Expiry.** `expires_at = issued_at + NXS_OTP__TTL_SECONDS` (default 300, bounded
60–1800), timezone-aware UTC. A verification at or after `expires_at` deterministically
materializes the row as `EXPIRED`, emits `otp.challenge.expired` and raises
`NXS_OTP_EXPIRED` — no successful transition is possible.

**Single-use / replay resistance.** `OtpService.verify` runs in ONE tenant transaction
that opens with `SELECT … FOR UPDATE` on the challenge row. Concurrent submissions
therefore serialize on the row lock: the first correct code commits the terminal
`VERIFIED` transition, and every later submission — correct or not — re-reads the
committed terminal state and is rejected (`NXS_OTP_ALREADY_USED`). Proven against real
PostgreSQL: six concurrent correct verifications yield exactly one success and
`attempts == 1` (the losers never counted).

**Attempt limits.** Each wrong code increments `attempts` atomically inside that same
locked transaction. Reaching `NXS_OTP__MAX_ATTEMPTS` (default 5, bounded 1–10) transitions
the row to `LOCKED` and emits `otp.challenge.failed_attempt` + `otp.challenge.locked`;
thereafter every submission — including the correct code — raises `NXS_OTP_LOCKED`.
Twelve concurrent wrong attempts leave `attempts == 5` exactly and status `LOCKED`.

**Commit-then-raise.** The transaction body computes the outcome and performs every
mutation (expiry materialization, attempt increment, lockout, `VERIFIED`); the
deterministic error, if any, is raised only *after* the transaction commits. A rejected
attempt is thus always durably counted — raising inside the `async with` would roll the
increment back and make brute-force unbounded.

Consequences: exactly-once acceptance and a hard attempt ceiling are database-enforced
invariants, not application hopes; a crash mid-verify either commits the whole step or
none of it.
