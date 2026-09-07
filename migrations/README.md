# Database migrations

Alembic is configured (NXS-MIGRATE-001) and wired to the validated
`NXS_DATABASE__DSN` through `migrations/env.py` (async engine, `compare_type` and
`compare_server_default` enabled). No credentials are committed.

## Commands

```bash
make migrate         # alembic upgrade head
make migrate-check   # upgrade head, then `alembic check` (fails on un-migrated model drift)
uv run alembic revision --autogenerate -m "add <table>"
uv run alembic downgrade -1
```

Revision files are named `YYYYMMDD_HHMM-<rev>_<slug>.py` and auto-formatted by Ruff.

## Policy

Every migration is forward-only, transaction-aware, backward-compatible with the
previously deployed release, and paired with roll-forward / rollback notes in its PR
(see `docs/engineering/standards.md`). P01 ships the framework only — there are no
business tables yet, so `migrations/versions/` is intentionally empty and
`alembic check` passes against the empty metadata.
