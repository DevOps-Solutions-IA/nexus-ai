# P19 SIP reference laboratory — implementation in progress

This directory is development/test infrastructure, not a deployment. P19 remains
BUILDING/PENDING. No capacity, availability, active-call failover, production or
full protocol certification is claimed.

## Artifact provenance

The repository-owned image builds official Kamailio 6.1.4 source with SHA-256
`290624b6624edc230af0fc458fb7e481e64b893be7cb5084398e587942992e0a`.
The build stage uses a digest-pinned Python/Debian image and dated signed package
snapshot. The runtime uses the repository's digest-pinned Wolfi Python image.
The Asterisk fixture uses a digest-pinned Wolfi base and exact package versions.
Earlier Debian runtime scans failed and were not accepted; the current Wolfi
runtime scans found zero HIGH/CRITICAL vulnerabilities with pinned Trivy 0.74.0.
This does not replace review of source-built Kamailio/Asterisk vulnerabilities.

Development builds:

```sh
docker buildx build --platform linux/amd64 --load -f infrastructure/kamailio/Dockerfile -t nexus-p19-kamailio:6.1.4-development .
docker buildx build --platform linux/arm64 --load -f infrastructure/kamailio/Dockerfile -t nexus-p19-kamailio:6.1.4-arm64-wolfi-development .
docker buildx build --platform linux/amd64 --load -f infrastructure/kamailio/asterisk.Dockerfile -t nexus-p19-asterisk:22.11.0-development .
```

These tags are local build handles, not immutable release identifiers. Certification
must record the resulting image/config/manifest digests and source hashes. Rebuild
after Dockerfile changes. Both Kamailio architectures have executed native version,
configuration and real UDP wire tests. ARM64 runs under explicit host emulation;
this is protocol compatibility evidence, not ARM64 performance certification.

The Asterisk fixture builds publisher-checksummed 22.11.0 source. It is a disposable
ARI/PJSIP protocol fixture, not production Asterisk infrastructure.

## Trust and secret injection

Native core parser diagnostics can include entire hostile SIP messages. The reference
therefore restricts that component to critical diagnostics and retains explicit,
bounded status/category operational messages through `xlog`. It does not log SIP
payloads or credentials. Real malformed-message tests assert that injected credential
markers and called numbers are absent while rejection metadata remains observable.
This is a log-confidentiality boundary, not a suppression of failed validation gates.

The current reference is UDP on an explicitly isolated test network. Source-address
peer policy is not Internet-grade cryptographic peer authentication. Production
network enforcement and deployment remain outside this directory's certification.

Kamailio reads `/run/secrets/nxs-sip-edge.json` from a read-only secret mount. Required
fields include an edge UUID, a per-edge HMAC secret of at least 32 bytes encoded as
hex, a literal resolver address/port, explicit finite peer networks/directions,
approved ingress hosts, `isolated_test_network=true`, and a `limits` object containing
positive `messages_per_second` (at most 1000), `pending_resolvers` (at most 2), and
`dialogs` (at most 10000). These are defensive limits, not certified capacity.
Do not commit an instantiated secret file. Missing credentials, missing/invalid bounds,
or wildcard networks fail startup. Kamailio receives
no PostgreSQL credentials; its sole authority request path is the signed resolver API.

The resolver connection uses HTTPS with the explicitly mounted CA certificate
`/run/secrets/resolver-ca.pem`, certificate/IP verification and TLS 1.2 minimum.
HMAC binds the method, path, body, edge, boot, timestamp and nonce independently of
TLS. Resolver requests have a 1.8-second absolute deadline and shorter socket
timeouts. An untrusted certificate or lost committed response fails closed.
Outbound peer configuration also requires finite `edge_hosts`; a SIP URI cannot
select an arbitrary upstream host. The separate `NXS_SIP_TOPOLOGY_KEY` secret must
contain 64 hexadecimal characters. Native topoh masks signaling route/Contact/Via
information; it does not grant dialog authority or claim SDP/RTP privacy.

The integration fixtures run UID/GID 10001 with all capabilities dropped, no privilege
escalation, a read-only root filesystem and a small writable runtime tmpfs. They do
not mount a Docker socket, use privileged mode or share the host PID namespace.
The test runner controls disposable containers from outside those containers.

## P11 internal originate seam

An existing durable P11 call can obtain an opaque P19 permit through a trusted internal
issuer. The Asterisk adapter uses the operations-owned Local channel context
`nxs-sip-egress` and the inherited creation-time variable `__NXS_SIP_EGRESS_PERMIT`.
Asterisk inheritance exposes `NXS_SIP_EGRESS_PERMIT` to the called channel. The
pre-dial handler in `asterisk-extensions.conf` places only that opaque value in
`X-NXS-Egress-Permit` on the trusted PJSIP leg. Public `CreateCallRequest` does not
accept this value. Caller-ID, From and source IP do not substitute for the permit.

Kamailio removes internal headers before relay. The direct real Asterisk test proves
ARI-to-PJSIP propagation and log non-disclosure. `test_sip_egress_wire.py` additionally
executes P11 durable call creation, permit issuance, real ARI/PJSIP, Kamailio consumption
and approved carrier-UAS reception with the internal permit removed. This is not
production Asterisk certification or completion of the entire attack matrix.

## Current executable checks and remaining work

`tests/integration/test_sip_wire.py` runs actual Kamailio, an internal HTTPS resolver,
PostgreSQL and UDP UAS; it checks INVITE/ACK/CANCEL/BYE/re-INVITE/UPDATE/INFO/PRACK,
bidirectional routing, two edges, two Cells, target rotation, durable dialog observations,
TLS rejection, committed response loss, owner restart and zero-send rejection paths.
Native transaction state distinguishes legitimate SIP retransmissions from a new
initial relay grant. `test_sip_asterisk_wire.py` uses actual TLS ARI
and PJSIP. Run against disposable databases with the P19 schema and local images built.
Do not run competing test processes against the same NATS test streams.

Complete peer/upstream control certification, all C01–C32/AC01–AC43 mappings,
final current-source regression and clean-room certification
remain required. These development tests are not an implementation-complete gate.
