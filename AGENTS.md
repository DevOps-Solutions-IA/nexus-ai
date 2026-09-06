# Nexus AI Agent Protocol

This file is the mandatory universal entrypoint for Codex, Claude Code, DeepSeek CLI, Gemini CLI, and future coding agents.

## Authority

Do not trust conversational memory. GitHub, Git history, and machine-readable NXS state are authoritative, in that order. Never infer a READY state or fabricate evidence.

## Before implementation

1. Read this file.
2. Read `.nxs/project-state.json`.
3. Run `make nxs-validate-repo` to validate schemas and invariants.
4. Read `.nxs/execution-policy.json`.
5. Read `.nxs/requirements.json`.
6. Read `.nxs/phase-registry.json`.
7. Read the requested `.nxs/phases/<phase>.json` manifest.
8. Run `make nxs-preflight PHASE=<phase>` and continue only on PASS.
9. Inspect relevant records in `docs/adr/`.
10. Inspect the branch, HEAD, remotes, history, and working tree.
11. Implement only within the manifest and branch contract.

Status changes must use `python -m scripts.nxs_state`; locks use `python -m scripts.nxs_guard lock`; closure uses `make nxs-close`. A mandatory failed gate prevents READY/GO. Never merge `main`, deploy production, rewrite history, expose secrets, or bypass a gate without explicit human authorization and documented risk acceptance.

Read `docs/runbooks/phase-lifecycle.md` before acquiring or recovering a lock. Future component directories marked `PLANNED / NOT IMPLEMENTED` are architectural reservations, not working software.
