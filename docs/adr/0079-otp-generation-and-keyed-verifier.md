# ADR-0079: OTP code generation and the keyed verifier

Status: Accepted. Part of NXS-P10 (`NXS-OTP-001`).

**Generation.** `nexus_ai.otp.codes.generate_code` draws a uniform integer in
`[0, 10**length)` from `secrets.randbelow` (a CSPRNG) and zero-pads it. No
`random.Random`, no timestamp-derived or sequential codes, no seeded / predictable
generator. `code_length` is typed (`NXS_OTP__CODE_LENGTH`, default 6, bounded 6–10) so
the code space is explicit and never dangerously small; a regression test asserts the
draws are non-monotonic and near-uniform.

**Storage — keyed hash, never plaintext.** A challenge stores
`code_hash = HMAC-SHA256(pepper, canonical_context ‖ 0x1F ‖ code)` and its
`hash_version`. The plaintext code is never persisted, logged, put on an event or
returned by any API. Plain `SHA256(code)` is rejected: the ~10^6 code space is trivially
brute-forced offline, so the secret pepper is mandatory.

**Canonical context (binding).** `canonical_context` joins, with a fixed field order and
a `0x1F` separator for `hash_version` 1:
`"nxs-otp:v1"`, `organization_id`, `challenge_id`, `purpose`, `channel`, `destination`
(normalized). A code is therefore worthless against any other challenge, purpose, tenant
or destination even if the hash leaks.

**Verification.** `verify_code` recomputes the digest and compares with
`hmac.compare_digest` — constant time. Verification always uses the challenge's stored
`hash_version`, so bumping `CURRENT_HASH_VERSION` is a clean forward migration
(old challenges still verify).

**Pepper.** `NXS_OTP__PEPPER` — a `SecretStr`, ≥ 32 characters, absent from `repr` / logs
and never written to the database. A hardened environment (staging / production) fails
fast at startup when it is missing; local / test may set
`NXS_OTP__ALLOW_EPHEMERAL_PEPPER=true` for a per-process random pepper (rejected outside
local/test, and rejected together with an explicit pepper).

**Destination fingerprint.** `destination_fingerprint = HMAC-SHA256(pepper,
"nxs-otp-dest" ‖ channel ‖ destination)` — a stable keyed value used as the throttle /
resend / one-active-challenge key so the raw address is not the hot index key.

**Masking.** `mask_destination` is deterministic: email → first char + `***` + `@` +
masked first label + remaining labels; phone → last 4 digits kept, the rest `*`, a
leading `+` preserved. Enough for a UX prompt, never the full value.

Consequences: an attacker who exfiltrates the challenge table or the event stream still
cannot recover or reuse a code; changing the verifier is versioned; the pepper is a hard
production prerequisite.
