# Nexus AI — by DevOps Solutions IA

Nexus AI is the production-first foundation for a horizontally scalable, multi-tenant, omnichannel enterprise AI platform. The current implementation scope is **NXS-P00: Engineering Control System** plus a minimal ASGI runtime proving the toolchain and container path. Business capabilities remain planned and are not implemented.

## Start here

```bash
make bootstrap
make nxs-preflight PHASE=NXS-P00
make validate
make up
```

`make down` stops local dependencies and `make logs` follows them. Docker Desktop or a compatible Docker daemon is required for container checks.

## Authoritative execution protocol

Repository state—not conversational memory—controls execution. Every agent reads [AGENTS.md](AGENTS.md), validates `.nxs/`, runs the execution guard, and follows the active phase manifest before implementation. The requirements ledger preserves mandatory future scope; the phase registry determines permitted sequencing; evidence proves readiness.

Key commands:

```bash
make nxs-validate-repo
make nxs-preflight PHASE=NXS-P00
make nxs-gate PHASE=NXS-P00
make nxs-close PHASE=NXS-P00 IMPLEMENTATION_COMMIT=<sha>
```

See `docs/engineering/control-system.md` and `docs/runbooks/phase-lifecycle.md` for continuation and closure procedures.
