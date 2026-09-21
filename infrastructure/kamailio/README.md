# P19 SIP reference implementation — feature-branch certified

This directory is development/test infrastructure, not a deployment. P19 is closed
READY/GO on `feat/nxs-p19-sip-scaling`, binding externally audited implementation
`860d9a129adad75d0e4d7f8470c8a9d9f224a5be`. Fresh certification covers UDP/TCP/TLS,
pre-ARI admission recovery, C01–C32/AC01–AC43/T01–T10/A01–A07, clean-room and exact
implementation-head CI/Security. Evidence and historical failures remain under
`.nxs/evidence/NXS-P19/`. Main remains through P18; P19 is not merged or production
deployed. External closure audit is pending. Merge and P20 start are unauthorized;
no capacity, availability, active-call failover or production certification is claimed.

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
after Dockerfile changes. Both Kamailio architectures require native configuration
and UDP/TCP/TLS wire validation for this corrective. ARM64 uses explicit emulation;
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

The reference listens on UDP/TCP 5060 and TLS 5061 inside the operations-controlled
container network. Plain UDP/TCP require explicitly isolated links; source-address
policy is not cryptographic authentication. TLS requires CA validation and the exact
observed leaf-certificate SHA-256 approved by peer policy. `$proto` and native TLS
state supply observations; SIP headers never do. No transport fallback exists.

TLS identity/key/CA files are read-only mounts at `/run/secrets/sip/identity.pem`,
`identity.key` and `ca.pem`. Keys cannot be world-readable; none of these files may
be group/world-writable. Invalid material and expired local identities fail startup.
Fixtures generate disposable PKI; no key/certificate is embedded in Git or images.
TLS targets require a matching operations-controlled `tls_targets` host/port/pin
entry and immutable PostgreSQL target/upstream fingerprint. Provision pins before
activating revisions. Dialogs retain their original transport and peer identity;
changing a pin or transport requires a new immutable revision.
The same pin map includes approved TLS origin Contact endpoints for reverse dialog
requests; arbitrary Contact ports are not accepted. `contact_port` is operations-owned
for TCP/TLS peers, while authenticated source identity is independent of ephemeral
TCP connection ports. Native TLS certificate checks also apply after reconnect.

The source build uses checksummed OpenSSL 3.6.4 development libraries matching the
pinned Wolfi runtime, without disabling Kamailio's OpenSSL compatibility check.
`tls-connection-auth.patch` is an explicit derivative of upstream 6.1.4: upstream's
connection-out callback is skipped without an `onsend` message, including transaction
module sends. The patch supplies the actual connection receive context, invokes the
callback for asynchronous handshakes, and rejects before queued SIP writes when the
callback drops the connection. It never bypasses CA verification. The wrong-target-pin
test reproduces unauthorized sending without the patch and requires zero target SIP
messages with it. Artifact provenance includes the patch; this is not represented as
an unmodified upstream binary. TLS connection-domain matching and disabled TCP aliases
prevent reusing a server-domain connection as a validated outgoing client connection.

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
and approved carrier-UAS reception with the internal permit removed. This fixture
alone is not the entire attack matrix; the complete matrix is separately recorded
in closure-criteria.json. No production Asterisk certification is claimed.

## Executable certification surfaces

`tests/integration/test_sip_wire.py` runs actual Kamailio, an internal HTTPS resolver,
PostgreSQL and UDP UAS; it checks INVITE/ACK/CANCEL/BYE/re-INVITE/UPDATE/INFO/PRACK,
bidirectional routing, two edges, two Cells, target rotation, durable dialog observations,
TLS rejection, committed response loss, owner restart and zero-send rejection paths.
Native transaction state distinguishes legitimate SIP retransmissions from a new
initial relay grant. `test_sip_asterisk_wire.py` uses actual TLS ARI
and PJSIP. Run against disposable databases with the P19 schema and local images built.
Do not run competing test processes against the same NATS test streams.

`test_sip_stream_transports.py` exercises TCP/TLS certificate rejection, downgrade
rejection, two edges, reconnect, CANCEL and pinned dialogs. Egress wire tests use
real ARI/PJSIP and UDP/TCP/TLS carrier UAS. The canonical generator maps C01–C32,
AC01–AC43 and supplemental T01–T10 to executed nodes. Current-source regression,
clean-room and security evidence remain distinct from historical certificates,
exact-head GitHub validation and independent external audit. Local PASS is not closure.
