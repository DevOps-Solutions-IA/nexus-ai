# Infrastructure

Local dependencies are implemented in `compose.yaml`. Canonical main through P18
includes the P11 application-level Asterisk/ARI contract and P18 PostgreSQL-authoritative
Cell placement/admission foundation. These are not production infrastructure deployments.

Production Asterisk infrastructure is not deployed. P19 is BUILDING/PENDING on
`feat/nxs-p19-sip-scaling`. Its reference configuration and disposable Kamailio/Asterisk
protocol fixtures now exist under `infrastructure/kamailio/`; these are implementation
branch artifacts, not production infrastructure or canonical-main P19 capability.
Local executable proofs and historical attempts are distinguished under
`.nxs/evidence/NXS-P19/`; independent audit and closure remain separate gates.
Nomad and fleet orchestration are later scope. Production infrastructure
and deployment remain P32; no capacity or failover certification is implied.

See `docs/adr/0100-sip-edge-routing-authority.md` and
`docs/engineering/nxs-p19-sip-edge-scaling-design.md` for the P19 contract.
