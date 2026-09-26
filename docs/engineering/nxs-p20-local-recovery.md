# P20 offline recovery and partial continuation

P20 remains BUILDING/PENDING. This is development work, not local phase
certification, not a published implementation candidate, and not READY/GO.

## Recovery

The original `/home/wundah/nexus-ai` workspace was preserved. A local
`git clone --no-hardlinks` reconstructed all 29 changed files in
`/tmp/nxs-p20-local.aW3DKc/nexus-ai`. Paths, modes, types and contents match the
captured manifest. The P20-B source hash is
`4e513a337824151235dae839a231ab484930ad2065f4a8446f6e66d07c5a79f8`.

Local preservation checkpoint:
`b47c0fcabbd40f1b93edfca5a30dcad3e0bf4172`, parent
`b8a9c5c4d3dceb740eaec6c756d9a1c842253384`. It is not the NXS
implementation_commit. Canonical stale-lock recovery and acquisition succeeded;
no second nxs-start occurred. No network operation or push occurred.

The virtual environment was copied locally without changing the original.
Editable import paths and console-script interpreter paths were relocated to the
recovery workspace. Validation uses this workspace's modules, not the original.

## Current partial continuation

The signal collector has a finite source-registered registry and converts bounded
health results into typed facts. Error text and external names are discarded.
Only the NXS state probe is currently concrete; the required PostgreSQL, NATS,
Cell, SIP and statically allowlisted HTTP integrations remain incomplete.

The advisory reasoner uses provider-neutral P13 message contracts, never tenant
agent persistence. Its platform secret reader requires a protected directory,
non-symlink regular read-only secret, safe ownership and bounded bytes. Reasoning
is disabled by default. Output accepts only bounded categories, confidence,
evidence references and known runbook suggestions; it has no action dispatch.
The platform model client is an injected interface, not a certified live provider
integration. These utilities are not yet a complete P20 reasoning service.

The foundation import contract now permits exactly
`nexus_ai.agents.models.base`, preserving explicit denial of tenant runtime,
service, toolbridge and persistence. The former foundation-only assertion that
reasoning could never be enabled is replaced by strict boolean configuration;
the default remains disabled. This is the authorized scope expansion, not an
assertion that reasoning or full P20 is certified.

Incident lifecycle completion, execution lease/dispatch fencing, full operator
authorization, subsystem adapters and canonical C/AC certification remain pending.
No mutable execution adapter or external effect has been introduced.

## Actual validation and limitations

The historical P20-B full run is preserved separately: 2479 passed, zero failed or
skipped, 91.23% coverage. It binds the preservation checkpoint's source content,
not subsequent new modules or changed configuration/tests.

Fresh foundation revalidation: 171 passed and 48 setup errors. PostgreSQL socket
creation is denied by the execution sandbox (`PermissionError: [Errno 1] Operation
not permitted`). Migration and schema-guard checks have the same limitation.
Docker daemon socket access is also denied. No alternate socket, namespace or
permission workaround was attempted.

Fresh local unit/contract validation after adding the partial utilities: 197 passed,
zero failed or skipped. Lint and typing passed with the relocated environment.
The complete suite was attempted, encountered database setup errors, then stopped
progressing in the legacy ASGI concurrency suite and was interrupted. It is not a
PASS and provides no current full-suite coverage certification.

Clean-room and container certification cannot be claimed without their required
capabilities. The final implementation commit and implementation bundle have not
been created, because their local certification prerequisites have not passed.

## Historical environment failure

The earlier P20-B first full run had six ARM64 `exec format error` failures from
missing binfmt/QEMU registration. The preceding execution restored the canonical
local prerequisite and completed the unchanged full rerun successfully. Those
failure artifacts remain referenced by foundation evidence; they are not erased
or reclassified as passing source executions.
