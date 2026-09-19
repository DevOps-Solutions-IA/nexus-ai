# Infrastructure

Local dependencies are implemented in `compose.yaml`. Canonical main through P18
includes the P11 application-level Asterisk/ARI contract and P18 PostgreSQL-authoritative
Cell placement/admission foundation. These are not production infrastructure deployments.

Production Asterisk infrastructure is not deployed. Kamailio P19 remains
PLANNED/PENDING in governance preparation on `feat/nxs-p19-sip-scaling`; its future
reference configuration belongs under `infrastructure/kamailio/`, but none is created
by governance. Nomad and fleet orchestration are later scope. Production infrastructure
and deployment remain P32; no capacity or failover certification is implied.

See `docs/adr/0100-sip-edge-routing-authority.md` and
`docs/engineering/nxs-p19-sip-edge-scaling-design.md` for the P19 contract.
