# Oracle Ampere A1 (ARM64) host prerequisites

The first NXS LAB targets Oracle Cloud Ampere A1 (`linux/arm64`). Oracle is **not**
deployed in P02 — this is preflight/documentation debt closure so P05+ deployment work
starts from a known baseline. The production backend image already builds for
`linux/amd64` and `linux/arm64` (ADR-0037), gated in CI.

## Kernel / sysctl

- `vm.overcommit_memory = 1` — required by Valkey/Redis background saves; without it
  `BGSAVE`/`fork()` can fail under memory pressure. Set in `/etc/sysctl.d/99-nxs.conf`
  and `sysctl --system`. (Observed during P00/P01 local Valkey bring-up.)
- `net.core.somaxconn` and `vm.max_map_count` — raise per the eventual deployment ADR
  when the SIP edge / JetStream footprint is sized (P04, P19). Not required for P02.

## Container runtime

- Docker or Podman with `containerd` image store; QEMU/binfmt is **not** needed on a
  native ARM64 host (it is only needed on amd64 CI runners to emulate arm64).
- The image runs as UID/GID `65532` (non-root); ensure any bind-mounted paths are
  writable by that id or use named volumes.

## PostgreSQL

- PostgreSQL 17 (`postgres:17` publishes `linux/arm64`).
- Run `infrastructure/postgres/roles.sql` once as a superuser (or let the first-boot
  initdb hook run it) to create `nexus_migration` and `nexus_runtime`. Supply real
  passwords out of band — the values in that file are local/test only.
- The application connects as `nexus_runtime`; migrations run as `nexus_migration`.
  In staging/production the app refuses to start if its role can bypass RLS.

## Not in scope here

Cell scheduling (P18), SIP edge (P19), capacity sizing (P28) and the deployment
pipeline (P32). This runbook is prerequisites only.
