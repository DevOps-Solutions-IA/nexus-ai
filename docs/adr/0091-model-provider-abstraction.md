# ADR-0091: Provider-neutral model abstraction and the untrusted-model contract

Status: Accepted — NXS-P13 (`NXS-AGENT-001`), 2026-09-10.

## Context

The runtime must call language models from different vendors (OpenAI, Azure OpenAI,
OpenAI-compatible gateways, local servers, and future providers) without leaking a vendor
shape into the domain, the API, the events or the database, and without ever handing a
model authority it should not have.

## Decision

### The adapter is a normalizer, nothing else

`ModelProviderAdapter` (`nexus_ai.agents.models.base`) has exactly two methods —
`generate(request, secret, http) -> ModelResponse` and `stream(...) -> AsyncIterator`.
An adapter turns a Nexus-owned `ModelRequest` into a provider REST call and the
provider's reply into a Nexus-owned `ModelResponse`. It owns no tenancy, no tool
authority, no credential storage, no reasoning.

* `ModelRequest` / `ModelResponse` / `ModelUsage` / `ModelToolCall` / `ModelMessage` /
  `ModelFinishReason` are the neutral vocabulary. They carry **no** `api_key`,
  `authorization`, `base_url`, `endpoint` or `headers` field (contract-tested).
* Vendor header names (`xi-api-key`, `anthropic-version`, `x-goog-api-key`, …) appear
  nowhere in `nexus_ai.agents.models.*` (contract-tested).
* `known_model_providers()` is frozen at `("fake", "openai_compatible")` for P13.

### Credentials and the transport

* The API key lives in the **NXS-P07 vault**, keyed `ai-model:{account_id}`. It is read
  only at the adapter boundary and attached only as an `Authorization` header. It never
  reaches a table, a log, an event, a response body or the model.
* `provider_api_base` is the **trusted** REST origin, taken from the model provider
  account's `api_base` — SSRF-validated at registration (`DestinationPolicy`) and again
  by the governed transport. A caller may never supply an API key, a raw endpoint, an
  unrestricted `base_url` or an arbitrary `Authorization` header.
* Every adapter call goes through an injected `ModelHttpTransport`. In production this is
  `GovernedModelHttpTransport`, wrapping the NXS-P07 governed HTTP executor — so a model
  adapter physically cannot reach an arbitrary or internal URL.

### Model output is untrusted input

`OpenAiCompatibleModelProvider` and the runtime validate every field of a provider
reply: content is bounded, an unknown `finish_reason` is a `ModelError`, tool-call ids
are bounded and de-duplicated, arguments must parse to a bounded, shallow JSON object,
provider error bodies are reduced to a short safe `provider_code` and never surfaced.
Nothing model-controlled is ever `eval`'d, `exec`'d, deserialized to a class/module
reference, or used as a URL.

### The fake provider is a first-class provider

`FakeModelProvider` implements the same interface with a scripted, deterministic
behaviour set (normal, streaming, one/many tools, malformed/deep/unknown tool, timeout,
disconnect, rate-limit, malformed response, hang-for-cancellation, duplicate event, tool
loop). Its stateless registry form is a bounded echo. CI never exercises a live
credential.

## Consequences

* The public API and OpenAPI surface expose no model endpoint, key or raw payload.
* Certification is **CONTRACT-CERTIFIED** (wire-shape mapping proven against a fake
  transport), explicitly **not LIVE-PROVIDER-CERTIFIED**. Live evidence is never
  fabricated.
* A new provider is a new adapter module plus a registry entry; nothing else moves.
