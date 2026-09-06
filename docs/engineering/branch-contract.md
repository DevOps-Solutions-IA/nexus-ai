# Branch Contract

Every execution branch must define: phase ID, objective, requirement IDs, dependencies and preconditions; scope and non-scope; functional, security, concurrency, resilience and test criteria; evidence paths; rollback and observability; GO/NO-GO decision; and an objective READY definition.

## NXS-P00 contract

- Phase: NXS-P00; branch: `feat/nxs-p00-engineering-control-system`.
- Objective: permanent agent-neutral governance, evidence, CI, security, deterministic runtime and validation.
- Preconditions: empty private canonical repository, initialized `main`, no prior P00 READY state.
- Scope: manifest requirements and minimal health/version runtime only. Business capabilities and production deployment are excluded.
- Criteria: every mandatory manifest gate passes; malformed and conflicting execution states block; container runs non-root and healthy; an independent agent reconstructs status from repository files.
- Rollback: abandon the unmerged feature branch; `main` retains repository identity only.
- Observability: structured runtime lifecycle logs and health/version endpoints.
- READY: requirements VALIDATED, evidence present, implementation SHA recorded, status READY and decision GO.
