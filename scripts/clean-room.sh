#!/usr/bin/env bash
set -euo pipefail

repository_root="$(git rev-parse --show-toplevel)"
cd "$repository_root"
rm -rf .venv .pytest_cache .mypy_cache .ruff_cache
uv sync --frozen --all-groups
uv run python -m scripts.nxs_validate
uv run pytest -q
docker compose up -d --wait
docker build --pull --no-cache -t nexus-ai:clean-room .
container_id="$(docker run --detach --publish 127.0.0.1:18081:8080 nexus-ai:clean-room)"
cleanup() {
  docker rm --force "$container_id" >/dev/null 2>&1 || true
  docker compose down >/dev/null 2>&1 || true
}
trap cleanup EXIT
test "$(docker inspect --format '{{.Config.User}}' "$container_id")" = "65532:65532"
for attempt in {1..30}; do
  if curl --fail --silent http://127.0.0.1:18081/health >/dev/null; then
    exit 0
  fi
  sleep 1
done
exit 1
