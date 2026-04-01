#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$REPO_ROOT"

services=("$@")
if [[ ${#services[@]} -eq 0 ]]; then
  services=(node miner)
fi

docker compose up -d "${services[@]}"
