#!/usr/bin/env bash
set -euo pipefail

repository_root="$(git rev-parse --show-toplevel)"
cd "$repository_root"
rm -rf .venv .pytest_cache .mypy_cache .ruff_cache .coverage

uv sync --frozen --all-groups
uv lock --check
uv run python -m scripts.nxs_validate
uv run ruff format --check .
uv run ruff check .
uv run mypy

docker compose up -d --wait

NXS_DATABASE__DSN='postgresql+asyncpg://nexus_local:local-development-only@127.0.0.1:15432/nexus_local' \
  NXS_ENVIRONMENT=test uv run alembic upgrade head
NXS_DATABASE__DSN='postgresql+asyncpg://nexus_local:local-development-only@127.0.0.1:15432/nexus_local' \
  NXS_ENVIRONMENT=test uv run alembic check

uv run pytest

# Fall back to the legacy builder if a broken local buildx plugin fails the metadata probe.
docker build --pull -t nexus-ai:clean-room . \
  || DOCKER_BUILDKIT=0 docker build --pull -t nexus-ai:clean-room .
test "$(docker image inspect nexus-ai:clean-room --format '{{.Config.User}}')" = "65532:65532"

container_id="$(docker run --detach --network host \
  --env NXS_ENVIRONMENT=local \
  --env 'NXS_DATABASE__DSN=postgresql+asyncpg://nexus_local:local-development-only@127.0.0.1:15432/nexus_local' \
  --env 'NXS_CACHE__URL=redis://127.0.0.1:16379/0' \
  --env 'NXS_MESSAGING__URL=nats://127.0.0.1:14222' \
  nexus-ai:clean-room)"
cleanup() {
  docker rm --force "$container_id" >/dev/null 2>&1 || true
  docker compose down >/dev/null 2>&1 || true
}
trap cleanup EXIT

for attempt in $(seq 1 30); do
  if curl --fail --silent http://127.0.0.1:8080/health/live >/dev/null; then break; fi
  sleep 1
done
curl --fail --silent http://127.0.0.1:8080/health/live >/dev/null
for attempt in $(seq 1 30); do
  code="$(curl --silent --output /dev/null --write-out '%{http_code}' http://127.0.0.1:8080/health/ready)"
  if [ "$code" = "200" ]; then break; fi
  sleep 1
done
test "$(curl --silent --output /dev/null --write-out '%{http_code}' http://127.0.0.1:8080/health/ready)" = "200"
docker stop --time 15 "$container_id" >/dev/null

uv run python -m pytest tests/integration/test_agent_handoff.py -q --no-cov -p no:cacheprovider >/dev/null
echo "CLEAN_ROOM: PASS"
