SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help
PHASE ?= NXS-P00

.PHONY: help bootstrap up down logs validate format lint type test security nxs-preflight nxs-validate-repo nxs-gate nxs-close docker-build
help:
	@echo "Nexus AI engineering commands: bootstrap up down logs validate nxs-preflight nxs-gate nxs-close"

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
	uv run pytest

security:
	uv run bandit -q -lll -r src scripts
	uv run pip-audit

nxs-preflight:
	uv run python -m scripts.nxs_guard --phase "$(PHASE)"

nxs-validate-repo:
	uv run python -m scripts.nxs_validate

nxs-gate:
	uv run python -m scripts.nxs_gate --phase "$(PHASE)"

nxs-close:
	@test -n "$(IMPLEMENTATION_COMMIT)" || (echo "IMPLEMENTATION_COMMIT is required" && exit 2)
	uv run python -m scripts.nxs_close --phase "$(PHASE)" --implementation-commit "$(IMPLEMENTATION_COMMIT)"

docker-build:
	docker build --pull --no-cache -t nexus-ai:p00 .

validate: lint type test nxs-validate-repo security
