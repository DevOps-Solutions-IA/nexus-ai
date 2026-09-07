# Claude Code entrypoint

Claude Code uses the universal [AGENTS.md](AGENTS.md) protocol. `.nxs/project-state.json`, Git, and GitHub are authoritative. Do not create agent-specific state, infer status from chat, or bypass `make nxs-preflight PHASE=<phase>`.
