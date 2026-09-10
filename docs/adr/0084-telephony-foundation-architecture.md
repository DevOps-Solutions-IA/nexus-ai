# ADR-0084: Telephony Foundation — architecture, provider abstraction, Asterisk boundary, SIP security

Status: Accepted. Establishes NXS-P11 (`NXS-TEL-001`): the permanent, provider-neutral
call-control and SIP/telephony domain that future phases consume:

    PSTN / SIP provider → SIP / telephony edge → NXS Telephony Foundation
    → Call / Media session → (NXS-P12 ElevenLabs voice) → (NXS-P13 AI Agent Runtime)

**Scope.** P11 owns the call state machine, the provider adapter contract, the Asterisk
ARI integration boundary, inbound + outbound call control, DTMF, and the media-session
domain P12 attaches external voice to. P11 ships NO voice agent, NO AI reasoning, NO
campaigns / workflows / scheduler, NO human call-center UI, NO Kamailio scaling and NO
call recording.

**Package layout.** `nexus_ai.telephony` — `entities` (domain values + strict API
contracts), `errors` (`NXS_TELEPHONY_*`), `state_machine` (fold rules, ADR-0085),
`destinations` (E.164 + SIP-safety), `account_config` (bounded per-provider allow-list),
`events` (P04 payloads, NXS-EVENT-011), `providers/{base,fake,asterisk,registry}`,
`service` (`TelephonyService`), `webhooks` (`InboundTelephonyService`). Persistence is
`nexus_ai.domain.telephony` — 6 tenant-owned tables + repositories.

**Provider abstraction.** `TelephonyProviderAdapter` is a *normalizer*:
`create_outbound_call` / `hangup_call` / `send_dtmf` / `verify_webhook` /
`parse_webhook` / `normalize_provider_event`. Provider-specific code stays behind an
adapter; a `fake` adapter is a first-class provider. Every provider HTTP call goes
through an injected `TelephonyTransport` — in production `GovernedTelephonyTransport`
over the NXS-P07 `GovernedHttpExecutor` (SSRF-safe, TLS-verified, bounded timeout).

**Asterisk boundary — ARI only.** Nexus's telephony engine is **Asterisk 22 LTS**,
integrated through the **Asterisk REST Interface (ARI)** for application-level call
control (originate, hangup, DTMF, bridges, Stasis events). **AMI is not used from the
application** — it is a broad operational channel; any justified AMI use is out-of-band
ops tooling, out of P11 scope. **PJSIP** configures SIP endpoints / trunks *inside*
Asterisk; the application never writes PJSIP config or dialplan — a trunk / endpoint is
provisioned by operations and referenced from `telephony_accounts.configuration` by a
bounded alias only. The adapter runs no shell, emits no dialplan and never returns the
ARI credential. Inbound Stasis events reach Nexus as **signed webhooks** from an
operations-owned normalizing bridge, verified by `verify_signed_webhook`. Asterisk is
**not** added to the dev/test compose — the container baseline (pinned digests, non-root
`65532`, no privileged container) is unchanged; a future ops runbook documents the
Asterisk 22 LTS deployment with minimal SIP/ARI/AMI exposure.

**SIP security.** `destinations.canonicalize_destination` is the one place a
caller-supplied destination becomes a trusted target: NFKC-normalized, control-char
free, and refused outright if it contains `\r` `\n` `;` `<` `>` `"` `sip:` `sips:`
`tel:` `@` whitespace or `\` (SIP-URI / header-injection machinery). A phone destination
is canonicalized to E.164 via the ONE product phone vocabulary (NXS-P06
`normalize_phone`); anything else must match a strict internal endpoint alias
(`^[a-z][a-z0-9._-]{1,63}$`) — never a URI, host or `user@host`. Four concerns are kept
separate: **display caller ID** (never caller-supplied; from an owned PhoneNumber row),
**provider account** (by id), **routing destination** (canonicalized here), **internal
SIP endpoint** (from account config only). A user-supplied `From` / `Contact` / `Route`
/ `Authorization` header is never trusted or forwarded.

**Caller-ID governance.** `CreateCallRequest` has **no `from` string** — it carries a
`from_number_id` referencing an Organization-owned, provider-verified
`telephony_phone_numbers` row (E.164, `verified = true`), resolved internally. Caller-ID
spoofing through an untrusted API value is structurally impossible.

**Bounded account configuration.** `account_config.validate_account_configuration`:
an allow-list per `(provider)` (`ari_base` / `ari_app` / `stasis_app` /
`default_country` / `sip_endpoint_prefix` for Asterisk), typed bounds (key count /
length, value length, serialized bytes), and `ari_base` must be a bare `https://`
origin (no credentials, query or fragment) — still forced through the P07
`DestinationPolicy` at call time. No per-send URL / method / header surface.

**RBAC.** `telephony:read` / `telephony:call` / `telephony:hangup` /
`telephony:configure`, deterministic ids `b2000000-…-000000000026..29`, seeded as a
migration delta: owner + admin get all four; `org_member` gets `read` + `call`.
`ROLE_PERMISSIONS` (frozen at P03) is unchanged.

Consequences: P12 attaches a voice agent to a `MediaSession` and consumes the frozen
call contract without redesigning P11; a carrier swap is an adapter, not a rewrite.
