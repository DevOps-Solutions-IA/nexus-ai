#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
artifacts=".nxs/runtime/sip-images"
mkdir -p "$artifacts"
docker buildx build --load --pull --platform linux/amd64 \
  --metadata-file "$artifacts/kamailio-amd64.json" \
  -f infrastructure/kamailio/Dockerfile \
  -t nexus-p19-kamailio:6.1.4-development .
docker buildx build --load --pull --platform linux/amd64 \
  --metadata-file "$artifacts/asterisk-amd64.json" \
  -f infrastructure/kamailio/asterisk.Dockerfile \
  -t nexus-p19-asterisk:22.11.0-development .
docker buildx build --load --pull --platform linux/arm64 \
  --metadata-file "$artifacts/kamailio-arm64.json" \
  -f infrastructure/kamailio/Dockerfile \
  -t nexus-p19-kamailio:6.1.4-arm64-wolfi-development .
for image in nexus-p19-kamailio:6.1.4-development \
  nexus-p19-kamailio:6.1.4-arm64-wolfi-development \
  nexus-p19-asterisk:22.11.0-development; do
  test "$(docker image inspect "$image" --format '{{.Config.User}}')" = '10001:10001'
  docker image inspect "$image" --format '{{.Id}} {{.Architecture}} {{.Config.User}}'
done
