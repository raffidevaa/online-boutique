#!/usr/bin/env bash
# Fetches the grpc_health_probe binary used by docker-compose.yml healthchecks for every
# gRPC-based Online Boutique service. Not committed to git (it's a binary) — run this once
# after cloning, or whenever bumping GRPC_HEALTH_PROBE_VERSION.
#
# grpc_health_probe is a statically linked Go binary (no libc dependency), so the same file
# works when bind-mounted read-only into any of the upstream images regardless of their base
# distro, including distroless/scratch ones that have no shell of their own.
set -euo pipefail

GRPC_HEALTH_PROBE_VERSION="v0.4.35"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARCH="$(uname -m)"

case "$ARCH" in
  x86_64) GOARCH="amd64" ;;
  aarch64|arm64) GOARCH="arm64" ;;
  *) echo "Unsupported architecture: $ARCH" >&2; exit 1 ;;
esac

URL="https://github.com/grpc-ecosystem/grpc-health-probe/releases/download/${GRPC_HEALTH_PROBE_VERSION}/grpc_health_probe-linux-${GOARCH}"
DEST="${SCRIPT_DIR}/grpc_health_probe"

curl -sL -o "$DEST" "$URL"
chmod +x "$DEST"
echo "Downloaded grpc_health_probe ${GRPC_HEALTH_PROBE_VERSION} (${GOARCH}) to ${DEST}"
