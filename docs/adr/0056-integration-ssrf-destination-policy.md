# ADR-0056: Integration SSRF destination policy and governed HTTP executor

Status: Accepted. Context: NXS-INT-001 lets tenants configure outbound HTTP destinations. Without a hard boundary that is a server-side request forgery vector into cloud metadata, internal services and the loopback interface. Decision:

**Deny-by-default destination policy** (`nexus_ai.integrations.destination.DestinationPolicy`), enforced at BOTH configuration time (base URL, OAuth token URL, OpenAPI server URL, webhook target) and execution time:

- scheme allow-list — only `http` and `https` (`file`, `ftp`, `gopher`, `data`, `ssh`, `ws`, … rejected);
- no credentials in the URL, no fragment, valid port;
- literal-IP hosts are classified directly; hostnames are classified by name (`localhost`, `*.localhost`, `*.local`, `*.internal`, `*.intranet`, `*.lan`, `*.home.arpa`) and validated against a strict hostname regex;
- blocked address space: loopback, link-local (including the `169.254.169.254` cloud-metadata address and `fd00:ec2::254`), private (RFC 1918 / unique-local), carrier-grade NAT (`100.64.0.0/10`), multicast, reserved, unspecified, documentation ranges, `0.0.0.0/8`, and any address Python's `ipaddress` does not consider globally routable; IPv4-mapped and NAT64 forms of any of the above are unwrapped and re-checked;
- a per-integration `DestinationRule` can only make the policy STRICTER — add blocked hosts / CIDRs, force https — never open one;
- staging/production require https for every outbound destination (`NXS_INTEGRATIONS__REQUIRE_HTTPS_OUTBOUND`, fail-closed at startup).

**DNS-to-private and DNS rebinding.** At execution time `resolve()` re-validates the URL, resolves the host and classifies EVERY returned address; if any is blocked the call is refused. The governed executor then connects to a pinned, already-validated address and carries the real hostname as `sni_hostname` so TLS stays correct — nothing re-resolves between check and connect.

**Governed HTTP executor** (`GovernedHttpExecutor`) — the only place Nexus opens an outbound integration connection:

- bounded connect / read / write timeouts AND a hard total-time ceiling (`asyncio.timeout`);
- a request-body size limit and a STREAMED response-body size limit (the limit is checked before the body is buffered);
- TLS certificate verification always on, not disableable; `trust_env=False`;
- redirects disabled by default; a bounded count re-validates each hop through the policy; an un-followed 3xx is `NXS_INT_REDIRECT_BLOCKED`;
- reserved / hop-by-hop headers stripped, a fixed `User-Agent` set;
- failures mapped to a small typed set (`FailureKind`: `PRE_SEND`, `IN_FLIGHT`, `UPSTREAM_5XX`, `UPSTREAM_429`, `TERMINAL`) so the retry policy and the error taxonomy have a stable contract. No raw upstream body or header is ever logged.

**Test escape hatch.** `DestinationPolicy(allow_loopback=True)` permits loopback ONLY (metadata, RFC 1918, link-local stay blocked) so tests can target a local mock server. Production wiring never passes it; the security matrix asserts the default policy blocks loopback.

Consequences: an integration can only ever reach a public `http(s)` endpoint; a rebind attack, a metadata-endpoint URL, an internal-service URL and a non-http scheme are all refused with a stable `NXS_INT_DESTINATION_BLOCKED` (config time) or the same code / `POLICY_BLOCKED` result class (execution time).
