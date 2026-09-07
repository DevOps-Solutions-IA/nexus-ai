# Contributing

All work follows `AGENTS.md` and the active `.nxs/phases/` manifest. Start from updated `main`, create the registered phase branch, acquire the execution lock, and require a PASS from `make nxs-preflight PHASE=<id>` before implementation.

Commits must be atomic, imperative, and traceable to requirement IDs. Pull requests must remain phase-scoped, contain failure-path tests, and link generated evidence. Generated files, dependency changes, migrations, compatibility effects, rollback behavior, and security impact must be explicit. Never weaken a gate to make a change pass.

Two-person review is preferred for security, tenancy, migrations, public contracts, and production infrastructure. Merge and production deployment require human authorization. See `docs/engineering/standards.md` and `docs/engineering/branch-contract.md`.
