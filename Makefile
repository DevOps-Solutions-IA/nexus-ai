SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

# PHASE is required by state-changing targets. Read-only targets infer it from the branch.
PHASE ?=
ACTOR ?= $(shell git config user.name 2>/dev/null || echo agent)
IMAGE_TAG ?= nexus-ai:$(shell git rev-parse --short HEAD 2>/dev/null || echo dev)

define require_phase
	@test -n "$(PHASE)" || { echo "PHASE is required, e.g. 'make $@ PHASE=NXS-P01'"; exit 2; }
endef

.PHONY: help bootstrap up down logs format lint type test test-integration security \
        test-all db-bootstrap nxs-schema-guard nxs-preflight nxs-validate-repo nxs-start \
        nxs-gate nxs-close nxs-phase nxs-lock-status nxs-lock-acquire nxs-lock-release \
        nxs-lock-recover docker-build docker-buildx-multiarch migrate migrate-check \
        clean-room validate

# Local database DSNs (roles created by `make db-bootstrap`). Override for other envs.
LOCAL_RUNTIME_DSN ?= postgresql+asyncpg://nexus_runtime:local-runtime-only@127.0.0.1:15432/nexus_local
LOCAL_MIGRATION_DSN ?= postgresql+asyncpg://nexus_migration:local-migration-only@127.0.0.1:15432/nexus_local
DB_ENV := NXS_ENVIRONMENT=test NXS_DATABASE__DSN=$(LOCAL_RUNTIME_DSN) NXS_DATABASE__MIGRATION_DSN=$(LOCAL_MIGRATION_DSN)

help:
	@echo "Nexus AI engineering commands:"
	@echo "  bootstrap up down logs                     - local toolchain and infrastructure"
	@echo "  format lint type test test-integration security - quality gates"
	@echo "  migrate migrate-check                      - database migrations"
	@echo "  nxs-phase                                  - print the phase for the current branch"
	@echo "  nxs-preflight PHASE=<id>                   - execution guard"
	@echo "  nxs-start PHASE=<id> ACTOR=<agent>         - begin an eligible phase"
	@echo "  nxs-gate PHASE=<id>                        - run and record the quality gate"
	@echo "  nxs-close PHASE=<id> IMPLEMENTATION_COMMIT=<sha> - two-commit closure"
	@echo "  nxs-lock-status|acquire|release|recover    - execution lock lifecycle"
	@echo "  docker-build clean-room validate           - reproduction and aggregate gate"

bootstrap:
	uv sync --frozen --all-groups

up:
	docker compose up -d --wait

down:
	docker compose down

logs:
	docker compose logs -f

format:
	uv run ruff format .
	uv run ruff check --fix .

lint:
	uv run ruff format --check .
	uv run ruff check .

type:
	uv run mypy

test:
	uv run pytest -m "not integration" --cov-fail-under=0

test-integration:
	$(DB_ENV) uv run pytest -m integration --cov-fail-under=0

test-all:
	$(DB_ENV) uv run pytest

security:
	uv run bandit -q -lll -c pyproject.toml -r src scripts
	uv run pip-audit

db-bootstrap:
	uv run python -m scripts.nxs_dbadmin bootstrap

migrate:
	$(DB_ENV) uv run alembic upgrade head

migrate-check:
	$(DB_ENV) uv run alembic upgrade head
	$(DB_ENV) uv run alembic check

nxs-schema-guard:
	$(DB_ENV) uv run python -m scripts.nxs_schema_guard

docker-buildx-multiarch:
	docker buildx build --platform linux/amd64,linux/arm64 --pull --tag "$(IMAGE_TAG)" .

nxs-phase:
	@uv run python -m scripts.nxs_guard current-phase

nxs-preflight:
	$(call require_phase)
	uv run python -m scripts.nxs_guard --phase "$(PHASE)"

nxs-validate-repo:
	uv run python -m scripts.nxs_validate

nxs-start:
	$(call require_phase)
	uv run python -m scripts.nxs_start --phase "$(PHASE)" --actor "$(ACTOR)"

nxs-gate:
	$(call require_phase)
	uv run python -m scripts.nxs_gate --phase "$(PHASE)"

nxs-close:
	$(call require_phase)
	@test -n "$(IMPLEMENTATION_COMMIT)" || { echo "IMPLEMENTATION_COMMIT is required"; exit 2; }
	uv run python -m scripts.nxs_close --phase "$(PHASE)" --implementation-commit "$(IMPLEMENTATION_COMMIT)"

nxs-lock-status:
	uv run python -m scripts.nxs_guard lock status

nxs-lock-acquire:
	$(call require_phase)
	uv run python -m scripts.nxs_guard lock acquire --phase "$(PHASE)" --actor "$(ACTOR)"

nxs-lock-release:
	$(call require_phase)
	uv run python -m scripts.nxs_guard lock release --phase "$(PHASE)"

nxs-lock-recover:
	uv run python -m scripts.nxs_guard lock recover

docker-build:
	docker build --pull --tag "$(IMAGE_TAG)" .

clean-room:
	scripts/clean-room.sh

validate: lint type test nxs-validate-repo security
